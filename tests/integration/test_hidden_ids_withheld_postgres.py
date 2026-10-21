"""The filter that withholds the id of a row a key may not read, through the mounted application on PostgreSQL.

* A POST that carries ``user_id`` only in its JSON body is filtered under ``APP_ENV`` development and test, where nothing
  rewrites the query. The filter reads rows for the user the key was authenticated for, not for the ``user_id`` of the query.
* The workspace task view withholds the id of the report a task made once the report is out of the key's reach.
* A commit that names a hidden id and a commit that names a missing id are answered alike, and read back alike. The id the
  caller sent stays in the answer to the commit where the answer repeats the input that held it, because it is not a row the
  caller discovered. An entry the door refuses is answered with the validation error of the request, which repeats the input
  as sent for a hidden id and a missing id alike; a stored legacy entry is read back alike for both.
* An id the caller supplied is no exemption anywhere else: a query parameter the route does not use, or an id in a field
  the answer does not repeat the input at, confirms nothing about a row the key may not read.
* Every refusal a door gives (a validation error, an invalid request, a 404, an unknown argument) is the same whole answer
  for the id of a row the key may not read and for an id that names no row: the status and the body, once generated values
  are normalised.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode
from uuid import uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_hidden_ids import apply_hidden_id_withholding
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.hidden_ids_postgres_support import normalize
from tests.support.legacy_source_refs import link_strings, needs_legacy_plant, plant_legacy_source_refs


def _memory(store, key, *, sensitivity="public", metadata=None):
    return store.create_memory(
        {
            "memory_key": key,
            "canonical_text": f"{key} kiln note",
            "title": key,
            "status": "active",
            "domain": "project",
            "sensitivity": sensitivity,
            "metadata_json": metadata or {},
        }
    )


def _post_with_user_in_the_body_only(path, payload, key, user_id, *, headers=()):
    """A POST whose query string is empty. The user is named in the JSON body and nowhere else."""

    messages: list[dict] = []
    body = json.dumps({"user_id": str(user_id), **payload}).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {key}".encode()),
            *headers,
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_a_post_with_user_id_only_in_its_body_is_filtered(label_harness, monkeypatch, app_env):
    """Nothing rewrites the query in these environments, so a filter that reads ``user_id`` from the query never runs.

    Mutation: ``withhold_json_body`` reads the user from ``request.query_params``. The hidden id stays in the answer.
    """

    h = label_harness
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
        shown = [
            _memory(store, f"shown{n}", metadata={"candidate_memory_ids": [str(hidden["id"])]}) for n in range(3)
        ]
    hidden_id = str(hidden["id"])
    trusted = h.key("trusted_local_agent")
    settings = Settings(database_url=h.urls["app"], app_env=app_env)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    first = "forget" if app_env == "development" else "expire"
    payload = {"memory_id": str(shown[0]["id"]), "reason": "r"}
    status, body = _post_with_user_in_the_body_only(f"/v0/vnext/memories/{first}", payload, trusted, h.user_id)
    assert status == 200, body
    assert body["memory"]["metadata_json"]["candidate_memory_ids"] == []
    assert hidden_id not in json.dumps(body)
    # The owner of the key's user is not filtered: an unbound admin key is shown the id as stored.
    admin = h.key("admin_agent")
    status, body = _post_with_user_in_the_body_only(
        "/v0/vnext/memories/expire", {"memory_id": str(shown[1]["id"]), "reason": "r"}, admin, h.user_id
    )
    assert status == 200, body
    assert body["memory"]["metadata_json"]["candidate_memory_ids"] == [hidden_id]


def test_a_post_in_production_is_filtered_for_the_user_of_the_header(label_harness, monkeypatch):
    h = label_harness
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
        shown = _memory(store, "shown", metadata={"candidate_memory_ids": [str(hidden["id"])]})
    trusted = h.key("trusted_local_agent")
    settings = Settings(database_url=h.urls["app"], app_env="production")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    status, body = _post_with_user_in_the_body_only(
        "/v0/vnext/memories/forget",
        {"memory_id": str(shown["id"]), "reason": "r"},
        trusted,
        h.user_id,
        headers=[(b"x-alicebot-user-id", str(h.user_id).encode())],
    )
    assert status == 200, body
    assert str(hidden["id"]) not in json.dumps(body)


def _queued_report(h, admin):
    with h.store() as store:
        task = VNextQueueService(store).enqueue_task(
            QueueTaskRequest(
                title="Summarize", task_type="summarize", instructions="Do it.", allowed_sources_json=[],
                domain="project", sensitivity="public",
            )
        )
    status, body, _ = h.request("POST", "/v0/vnext/queue/process-next", payload={}, key=admin)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == str(task["id"]), body
    return str(task["id"]), body["artifact_id"]


def _task(h, key, task_id):
    status, body, _ = h.request("GET", "/v0/vnext/workspace", key=key)
    assert status == 200, body
    return next(item for item in body["tasks"] if item["id"] == task_id)


def test_the_workspace_task_view_withholds_the_report_a_task_made(label_harness):
    """A task names the report it made in ``output_artifact_id`` and in its metadata. The report is made confidential since.

    Mutation: ``output_artifact_id`` leaves the typed scalar fields, or the artifact leaves the kinds the filter reads.
    """

    h = label_harness
    admin = h.key("admin_agent")
    trusted = h.key("trusted_local_agent")
    task_id, artifact_id = _queued_report(h, admin)
    before = _task(h, trusted, task_id)
    assert before["output_artifact_id"] == artifact_id and before["metadata_json"] == {"artifact_id": artifact_id}
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("UPDATE generated_artifacts SET sensitivity = 'confidential' WHERE id = %s::uuid", (artifact_id,))
    after = _task(h, trusted, task_id)
    assert after["output_artifact_id"] is None and after["metadata_json"] == {"artifact_id": None}
    assert artifact_id not in json.dumps(h.request("GET", "/v0/vnext/workspace", key=trusted)[1])
    # The task is otherwise as it was, and an unbound admin key is shown the id as stored.
    assert {k: v for k, v in after.items() if k not in {"output_artifact_id", "metadata_json"}} == {
        k: v for k, v in before.items() if k not in {"output_artifact_id", "metadata_json"}
    }
    owner = _task(h, admin, task_id)
    assert owner["output_artifact_id"] == artifact_id and owner["metadata_json"] == {"artifact_id": artifact_id}


def test_the_workspace_task_view_withholds_a_report_id_that_names_no_report(label_harness):
    """The metadata of a task holds no foreign key, so it can name a report that is gone. That answers as a hidden one does."""

    h = label_harness
    admin = h.key("admin_agent")
    trusted = h.key("trusted_local_agent")
    task_id, artifact_id = _queued_report(h, admin)
    answers = {}
    for label, value in (("hidden", artifact_id), ("missing", str(uuid4()))):
        with h.store() as store, store.conn.cursor() as cur:
            if label == "hidden":
                cur.execute("UPDATE generated_artifacts SET sensitivity = 'confidential' WHERE id = %s::uuid", (artifact_id,))
            cur.execute(
                "UPDATE task_queue SET metadata_json = jsonb_build_object('artifact_id', %s::text) WHERE id = %s::uuid",
                (value, task_id),
            )
        answers[label] = _task(h, trusted, task_id)
        assert value not in json.dumps(answers[label])
    assert answers["hidden"]["metadata_json"] == answers["missing"]["metadata_json"] == {"artifact_id": None}


def _post_commit(h, key, refs):
    return h.request(
        "POST",
        "/v0/vnext/memories/commit",
        key=key,
        payload={
            "title": "A kiln note",
            "canonical_text": f"Kiln note {uuid4()}",
            "source_refs": refs,
            "domain": "project",
            "sensitivity": "public",
        },
    )


def _commit(h, key, refs):
    status, body, _ = _post_commit(h, key, refs)
    assert status == 201, body
    return body["memory"]["id"], body


def _read_back(h, monkeypatch, key, memory_id):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    review = call_mcp_tool(context, name="alice_memory_review", arguments={"review_item_id": memory_id})
    explained = call_mcp_tool(context, name="alice_explain", arguments={"memory_id": memory_id})
    status, audit, _ = h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=key)
    assert status == 200, audit
    return {"review": review, "explain": explained, "audit": audit}


def _spellings_of(row_id):
    return [row_id, row_id.upper(), row_id.replace("-", ""), f"memory:{row_id}", f"urn:uuid:{row_id}"]


def _without_the_callers_id(answer, row_id):
    """An answer (status and body) with every spelling of the id the caller sent replaced, and generated values normalised.

    The caller's id is the one thing that differs between asking about a hidden row and asking about a missing one, so two
    answers are compared by what they say around it. An id that was withheld in one answer and written in the other still
    differs: ``null`` is not the placeholder.
    """

    status, body = answer
    text = json.dumps(body, sort_keys=True, default=str)
    for spelling in sorted(_spellings_of(row_id), key=len, reverse=True):
        text = re.sub(re.escape(spelling), "<CALLERS_ID>", text, flags=re.IGNORECASE)
    return status, normalize(json.loads(text))


def _same_answer(hidden_answer, hidden_id, missing_answer, missing_id):
    return _without_the_callers_id(hidden_answer, hidden_id) == _without_the_callers_id(missing_answer, missing_id)


def _without_events(value):
    """A trace and an audit list the policy events that each read writes, so two reads of one row differ by those."""

    if isinstance(value, dict):
        return {key: _without_events(child) for key, child in value.items() if key not in ("events", "event_count")}
    if isinstance(value, list):
        return [_without_events(child) for child in value]
    return value


# Whether the answer to the commit shows the id that a form the door accepts names.
ECHOED = [True, True]
# The form that names the id in a field the product does not read as a reference and without a prefix: it may name a
# chunk or a session, and the saved-quote reader holds it to the rule for rows that exist.
INCIDENTAL = 5


def _forms(row_id):
    """The first two are strings, which the door accepts. The others are entries no producer writes, which it refuses."""

    return [
        f"memory:{row_id}",
        f"MEMORY:{row_id.upper()}",
        {"memory_id": row_id, "quote": "q"},
        {"origin": f"memory:{row_id}"},
        [f"memory:{row_id}", {"nested": {"memory_id": row_id}}],
        {"see_also": row_id},
    ]


def test_a_commit_that_names_a_hidden_id_is_answered_and_read_back_as_one_that_names_a_missing_id(label_harness, monkeypatch):
    """A key that holds a candidate id commits a memory that cites it, and reads the memory back.

    The strings the door accepts are answered with the ids the caller sent, so a hidden id and a missing id both stand in
    the answer. The entries it refuses (an object, a list) are answered with the validation error of the request, which
    repeats the input as sent: the two refusals are the same answer. The vault a v0.20.2 release stored holds such entries,
    so each is planted as a stored row and read back. When the memory is read back the ids are not the caller's request any
    more: a hidden id and a missing id are both withheld, and the stored memory reads alike, field by field. The owner of
    the vault sees the stored entry, so the filter had something to withhold.

    Mutations: ``withhold_json_body`` is given no ``named_body`` (the answer to the commit loses the ids the caller sent);
    ``_Judge.withheld`` asks only whether the id is hidden in a typed position (a missing id stays when the memory is read
    back); the request flag is never set, or ``withhold_json_body`` does not read it (the refusal of a hidden id loses the
    id the caller sent, and the refusal of a missing id keeps it).
    """

    h = label_harness
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
    hidden_id, missing_id = str(hidden["id"]), str(uuid4())
    trusted = h.key("trusted_local_agent")
    admin = h.key("admin_agent")
    made = {}
    answers = {}
    refusals = {}
    for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
        for number, ref in enumerate(_forms(row_id)):
            if needs_legacy_plant([ref]):
                status, refusal, _ = _post_commit(h, trusted, [ref])
                assert status == 422, (label, number, refusal)
                # The refusal repeats what the caller sent, so the id stands in it.
                assert row_id in json.dumps(refusal).lower(), (label, number)
                refusals[(label, number)] = (status, refusal)
                # The vault of an earlier release holds the entry: commit what the door takes, then store the entry.
                sent = link_strings([ref])
                memory_id, _ = _commit(h, trusted, sent)
                with h.store() as store:
                    plant_legacy_source_refs(store, memory_id, [ref], sent)
            else:
                memory_id, answer = _commit(h, trusted, [ref])
                answers[(label, number)] = answer
                # The caller sent the id, so the answer to the commit shows it, hidden row or not.
                assert (row_id in json.dumps(answer).lower()) is ECHOED[number], (label, number)
            made[(label, number)] = memory_id
    for number in range(len(ECHOED)):
        assert normalize(answers[("hidden", number)]["memory"]["value"]) == normalize(
            answers[("missing", number)]["memory"]["value"]
        )
    for number in range(len(ECHOED), len(_forms(hidden_id))):
        assert _same_answer(refusals[("hidden", number)], hidden_id, refusals[("missing", number)], missing_id), number
    for number in range(len(_forms(hidden_id))):
        hidden_view = _read_back(h, monkeypatch, trusted, made[("hidden", number)])
        missing_view = _read_back(h, monkeypatch, trusted, made[("missing", number)])
        assert hidden_id not in json.dumps(hidden_view).lower()
        if number >= len(ECHOED):
            # The owner reads the stored entry as it was planted. Without it the rows below would hold nothing to withhold.
            for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
                assert row_id in json.dumps(_read_back(h, monkeypatch, admin, made[(label, number)])).lower(), (label, number)
        if number == INCIDENTAL:
            # An id under a field the product does not read as a reference may name a chunk or a session. The saved-quote
            # reader refuses it only when it names a stored row, and then it removes the copy of the quote beside it, and the
            # filter withholds only the id of a stored row there too. That is the boundary the security note states. The id of
            # the hidden row is gone, and the id of a missing row stands as the caller wrote it.
            assert missing_id in json.dumps(missing_view).lower()
            continue
        assert missing_id not in json.dumps(missing_view).lower()
        # Every id and instant is replaced before two answers are compared, so they compare by what they say.
        for door in ("review", "explain"):
            assert normalize(hidden_view[door]) == normalize(missing_view[door]), (number, door)
        assert normalize(hidden_view["audit"]["memory"]["value"]) == normalize(missing_view["audit"]["memory"]["value"])
        assert normalize(hidden_view["audit"]["memory"]["metadata_json"]) == normalize(
            missing_view["audit"]["memory"]["metadata_json"]
        )


def _a_hidden_id_and_a_missing_id(h):
    """The id of a memory the trusted key may not read, and the id of no row."""

    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
    return str(hidden["id"]), str(uuid4())


@pytest.mark.parametrize("number", [2, 3, 4, 5])
def test_a_source_ref_the_door_refuses_is_answered_alike_for_a_hidden_id_and_a_missing_id(label_harness, number):
    """The four entries no producer writes: an object that names the id, an object whose field holds a prefixed id, a list
    that holds both, and an object whose field the product does not read.

    The refusal is the 422 of a string field, and it repeats the entry as the caller sent it, the id included, for both.

    Mutations: the request flag is never set, or ``withhold_json_body`` does not read it; the flag is set for every 422
    (a unit test builds one).
    """

    h = label_harness
    hidden_id, missing_id = _a_hidden_id_and_a_missing_id(h)
    trusted = h.key("trusted_local_agent")
    answers = {}
    for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
        status, body, _ = _post_commit(h, trusted, [_forms(row_id)[number]])
        assert status == 422, body
        assert body["detail"][0]["type"] == "string_type" and body["detail"][0]["loc"] == ["body", "source_refs", 0]
        assert row_id in json.dumps(body).lower(), label
        answers[label] = (status, body)
    assert _same_answer(answers["hidden"], hidden_id, answers["missing"], missing_id)


def test_a_body_field_the_route_does_not_declare_is_refused_alike_for_a_hidden_id_and_a_missing_id(label_harness):
    """The commit takes no ``metadata`` field. The 422 says so and repeats the input as sent, in the field of the caller's own.

    Mutations: the request flag is never set, or ``withhold_json_body`` does not read it.
    """

    h = label_harness
    hidden_id, missing_id = _a_hidden_id_and_a_missing_id(h)
    trusted = h.key("trusted_local_agent")
    answers = {}
    for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
        status, body = _call(
            "POST", "/v0/vnext/memories/commit", query={}, key=trusted, user_id=h.user_id,
            payload={
                "user_id": str(h.user_id), "title": "A kiln note", "canonical_text": f"Kiln note {uuid4()}",
                "domain": "project", "sensitivity": "public", "metadata": {"see_also": row_id},
            },
        )
        assert status == 422, body
        assert body["detail"][0]["type"] == "extra_forbidden" and body["detail"][0]["loc"] == ["body", "metadata"]
        assert body["detail"][0]["input"] == {"see_also": row_id}
        answers[label] = (status, body)
    assert _same_answer(answers["hidden"], hidden_id, answers["missing"], missing_id)


def test_a_query_value_of_the_wrong_type_is_refused_alike_for_a_hidden_id_and_a_missing_id(label_harness):
    """``limit`` is an integer. An id in it is refused, and the 422 repeats the value as sent.

    Mutations: the request flag is never set, or ``withhold_json_body`` does not read it.
    """

    h = label_harness
    hidden_id, missing_id = _a_hidden_id_and_a_missing_id(h)
    trusted = h.key("trusted_local_agent")
    answers = {}
    for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
        status, body = _call(
            "GET", "/v0/vnext/artifacts", query={"limit": row_id}, payload=None, key=trusted, user_id=h.user_id
        )
        assert status == 422, body
        assert body["detail"][0]["type"] == "int_parsing" and body["detail"][0]["loc"] == ["query", "limit"]
        assert body["detail"][0]["input"] == row_id
        answers[label] = (status, body)
    assert _same_answer(answers["hidden"], hidden_id, answers["missing"], missing_id)


def _hidden_rows(store):
    """One row of each kind the doors below name, none of them readable by the trusted key."""

    memory = _memory(store, "hidden", sensitivity="confidential")
    source = store.create_source(
        {
            "source_type": "note", "title": "hidden source", "content_hash": uuid4().hex, "domain": "project",
            "sensitivity": "confidential", "metadata_json": {"project_scope": []},
        }
    )
    with without_insert_floor():
        artifact = store.create_artifact(
            {
                "artifact_type": "daily_brief", "title": "T", "content_markdown": "C", "domain": "project",
                "sensitivity": "confidential",
                "metadata_json": {
                    "derived_from": {
                        "v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [],
                        "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                    },
                },
            }
        )
    return {"memory": str(memory["id"]), "source": str(source["id"]), "artifact": str(artifact["id"])}


# (name, kind of the hidden row, the status of the answer). The call is built in the test.
REFUSALS = [
    ("commit with a json text entry", "memory", 422),
    ("commit with a blank entry beside a prefixed id", "memory", 400),
    ("proposal with an object entry", "memory", 422),
    ("artifacts list, limit", "artifact", 422),
    ("recent commits, limit", "memory", 422),
    ("context tree, limit", "memory", 422),
    ("quality evals, artifact", "artifact", 200),
    ("quality evals, artifact that is not an id", "artifact", 422),
    ("artifact", "artifact", 404),
    ("artifact, id that is not an id", "artifact", 422),
    ("audit of a memory", "memory", 404),
    ("audit of a memory, id that is not an id", "memory", 422),
    ("trace of a source", "source", 404),
    ("trace of a source, id that is not an id", "source", 422),
    ("trace of an artifact", "artifact", 404),
    ("trace of an artifact, id that is not an id", "artifact", 422),
    ("source", "source", 404),
    ("source, id that is not an id", "source", 422),
    ("graph neighborhood", "memory", 200),
    ("review of a memory", "memory", 404),
    ("review of a source, id that is not an id", "source", 422),
    ("forget, id that is not an id", "memory", 422),
    ("forget", "memory", 400),
    ("tool: commit with an object entry", "memory", "MCPArgumentError"),
    ("tool: commit with a json text entry", "memory", "MCPArgumentError"),
    ("tool: commit with an argument it does not take", "memory", "MCPArgumentError"),
    ("tool: explain", "memory", "MCPToolError"),
    ("tool: review, id that is not an id", "memory", "MCPArgumentError"),
    ("tool: recall, limit", "memory", "MCPArgumentError"),
    ("tool: context pack, ids it does not take", "memory", "MCPArgumentError"),
]


def _refusal_calls(h, key, monkeypatch):
    """name -> a function of the id the caller sends that returns the whole answer: (status, body)."""

    user = str(h.user_id)

    def commit_body(extra):
        return lambda row_id: {"user_id": user, "title": "t", "canonical_text": f"Kiln note {uuid4()}", "domain": "project",
                               "sensitivity": "public", **extra(row_id)}

    def post(path, body):
        return lambda row_id: _call("POST", path.format(id=row_id), query={}, payload=body(row_id), key=key, user_id=h.user_id)

    def get(path, query=None):
        return lambda row_id: _call("GET", path.format(id=row_id), query=(query or (lambda _: {}))(row_id), payload=None, key=key, user_id=h.user_id)

    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def tool(name, arguments):
        def run(row_id):
            try:
                return "ok", call_mcp_tool(context, name=name, arguments=arguments(row_id))
            except MCPToolError as error:
                return type(error).__name__, str(error)

        return run

    def not_an_id(call):
        return lambda row_id: call(row_id + "x")

    calls = {
        "commit with a json text entry": post("/v0/vnext/memories/commit", commit_body(lambda r: {"source_refs": [json.dumps({"memory_id": r})]})),
        "commit with a blank entry beside a prefixed id": post("/v0/vnext/memories/commit", commit_body(lambda r: {"source_refs": [f"memory:{r}", "   "]})),
        "proposal with an object entry": post("/v0/vnext/memory-proposals", commit_body(lambda r: {"source_refs": [{"memory_id": r}]})),
        "artifacts list, limit": get("/v0/vnext/artifacts", lambda r: {"limit": r}),
        "recent commits, limit": get("/v0/vnext/memories/recent-commits", lambda r: {"limit": r}),
        "context tree, limit": get("/v0/vnext/context-tree", lambda r: {"limit": r}),
        "quality evals, artifact": get("/v0/vnext/quality-evals", lambda r: {"artifact_id": r}),
        "quality evals, artifact that is not an id": not_an_id(get("/v0/vnext/quality-evals", lambda r: {"artifact_id": r})),
        "artifact": get("/v0/vnext/artifacts/{id}"),
        "artifact, id that is not an id": not_an_id(get("/v0/vnext/artifacts/{id}")),
        "audit of a memory": get("/v0/vnext/memories/{id}/audit"),
        "audit of a memory, id that is not an id": not_an_id(get("/v0/vnext/memories/{id}/audit")),
        "trace of a source": get("/v0/vnext/traces/sources/{id}"),
        "trace of a source, id that is not an id": not_an_id(get("/v0/vnext/traces/sources/{id}")),
        "trace of an artifact": get("/v0/vnext/traces/artifacts/{id}"),
        "trace of an artifact, id that is not an id": not_an_id(get("/v0/vnext/traces/artifacts/{id}")),
        "source": get("/v0/vnext/sources/{id}"),
        "source, id that is not an id": not_an_id(get("/v0/vnext/sources/{id}")),
        "graph neighborhood": get("/v0/vnext/graph/neighborhood/{id}"),
        "review of a memory": post("/v0/vnext/memories/{id}/review", lambda r: {"user_id": user, "action": "accept"}),
        "review of a source, id that is not an id": not_an_id(post("/v0/vnext/sources/{id}/review", lambda r: {"user_id": user, "action": "review"})),
        "forget, id that is not an id": post("/v0/vnext/memories/forget", lambda r: {"user_id": user, "memory_id": r + "x", "reason": "r"}),
        "forget": post("/v0/vnext/memories/forget", lambda r: {"user_id": user, "memory_id": r, "reason": "r"}),
        "tool: commit with an object entry": tool("alice_memory_commit", lambda r: {"title": "t", "canonical_text": f"Kiln note {uuid4()}", "source_refs": [{"memory_id": r}]}),
        "tool: commit with a json text entry": tool("alice_memory_commit", lambda r: {"title": "t", "canonical_text": f"Kiln note {uuid4()}", "source_refs": [json.dumps({"memory_id": r})]}),
        "tool: commit with an argument it does not take": tool("alice_memory_commit", lambda r: {"title": "t", "canonical_text": "c", "probe": r}),
        "tool: explain": tool("alice_explain", lambda r: {"memory_id": r}),
        "tool: review, id that is not an id": not_an_id(tool("alice_memory_review", lambda r: {"review_item_id": r})),
        "tool: recall, limit": tool("alice_recall", lambda r: {"query": "kiln", "limit": r}),
        "tool: context pack, ids it does not take": tool("alice_context_pack", lambda r: {"query": "kiln", "memory_ids": [r]}),
    }
    assert set(calls) == {name for name, _, _ in REFUSALS}
    return calls


def test_every_refusal_is_the_same_whole_answer_for_a_hidden_id_and_for_a_missing_id(label_harness, monkeypatch):
    """The status and the body are compared, after generated values are normalised, for every status.

    The doors: a body or a query the framework refuses (422), an entry the product refuses (400, and the tools' own
    errors), an id that names nothing (404), a tool argument it does not take, and an id in a path that is not an id. A
    422 repeats the caller's input as sent, so it carries a hidden id as it carries a missing one.

    Mutations: the request flag is never set, or ``withhold_json_body`` does not read it (the 422 of a hidden id loses the
    id and the 422 of a missing id keeps it).
    """

    h = label_harness
    with h.store() as store:
        rows = _hidden_rows(store)
    calls = _refusal_calls(h, h.key("trusted_local_agent"), monkeypatch)
    for name, kind, expected in REFUSALS:
        hidden_id, missing_id = rows[kind], str(uuid4())
        hidden_answer, missing_answer = calls[name](hidden_id), calls[name](missing_id)
        assert hidden_answer[0] == expected, (name, hidden_answer)
        assert _same_answer(hidden_answer, hidden_id, missing_answer, missing_id), (name, hidden_answer, missing_answer)


def _statements(h, identity, payload):
    """How many statements the filter runs for one payload, and what it returns."""

    seen: list[str] = []
    original = psycopg.Cursor.execute

    def counting(self, query, *args, **kwargs):
        seen.append(str(query)[:80])
        return original(self, query, *args, **kwargs)

    psycopg.Cursor.execute = counting
    try:
        with h.store() as store:
            seen.clear()
            shown = apply_hidden_id_withholding(store, identity, payload)
    finally:
        psycopg.Cursor.execute = original
    return len(seen), shown


@pytest.mark.parametrize("derived", [False, True])
def test_the_filter_runs_the_same_statements_for_ten_rows_and_for_a_thousand(label_harness, derived):
    """One lookup for each kind of row, whatever the number of ids, and the counts follow the list that remains.

    Half of the rows are readable and half are above the key's ceiling. With ``derived`` each row also names a source of its
    own, so the label walk has an input to settle for every row.

    Mutation: ``resolve_ids`` asks for the rows of one id at a time. The count grows with the rows and this fails.
    """

    h = label_harness
    identity = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", auth="agent_api_key")
    statements = {}
    for size in (10, 1000):
        readable, hidden = [], []
        with h.store() as store:
            for number in range(size):
                metadata = {}
                if derived:
                    source = store.create_source(
                        {
                            "source_type": "note", "title": f"s{number}", "content_hash": str(uuid4()), "domain": "project",
                            "sensitivity": "public", "metadata_json": {"project_scope": []},
                        }
                    )
                    metadata = {"source_id": str(source["id"])}
                readable.append(str(_memory(store, str(uuid4()), metadata=metadata)["id"]))
                hidden.append(str(_memory(store, str(uuid4()), sensitivity="confidential", metadata=metadata)["id"]))
        everyone = readable + hidden
        payload = {
            "metadata_json": {
                "memory_ids": everyone,
                "source_refs": [f"memory:{row_id}" for row_id in everyone],
                "derived_from": {"memories": everyone, "counts": {"memories": len(everyone)}},
            }
        }
        statements[size], shown = _statements(h, identity, payload)
        metadata = shown["metadata_json"]
        assert metadata["memory_ids"] == readable
        assert metadata["source_refs"] == [f"memory:{row_id}" for row_id in readable]
        assert metadata["derived_from"] == {"memories": readable, "counts": {"memories": size}}
    assert statements[10] == statements[1000] <= 8, statements


def _call(method, path, *, query, payload, key, user_id):
    """The mounted application, called with exactly this query string and, for a POST, this JSON body."""

    messages: list[dict] = []
    body = json.dumps(payload).encode() if payload is not None else b""
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": urlencode({"user_id": str(user_id), **query}).encode(),
        "headers": [
            (b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {key}".encode()),
        ],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


def test_a_query_parameter_the_route_does_not_use_confirms_no_association(label_harness):
    """A key that knows or guesses an id adds it to the query of a read. The report is answered as without it.

    The routes below declare ``user_id`` and, for the artifact list, a few filters of their own. None of them reads
    ``probe``, ``other``, ``candidate_memory_ids``, ``metadata_json`` or ``source_refs``, so the id they carry is no part
    of the key's input, also where the name is the name of a key of the report.

    Mutation: ``echo_of_request`` takes every id of the query, or ``Walk.withheld`` exempts the ids of the request wherever
    they stand. The id of the archived row stands in the report again.
    """

    h = label_harness
    admin = h.key("admin_agent")
    trusted = h.key("trusted_local_agent")
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
        shown = _memory(store, "shown", metadata={"candidate_memory_ids": [str(hidden["id"])]})
        with without_insert_floor():
            report = store.create_artifact(
                {
                    "artifact_type": "daily_brief", "title": "T", "content_markdown": "C", "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {
                        "candidate_memory_ids": [str(hidden["id"])],
                        "derived_from": {
                            "v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [],
                            "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                        },
                    },
                }
            )
    hidden_id = str(hidden["id"])
    doors = (
        f"/v0/vnext/artifacts/{report['id']}",
        f"/v0/vnext/traces/artifacts/{report['id']}",
        f"/v0/vnext/memories/{shown['id']}/audit",
        "/v0/vnext/workspace",
        "/v0/vnext/artifacts",
    )
    missing_id = str(uuid4())
    refused = 0
    for path in doors:
        status, plain = _call("GET", path, query={}, payload=None, key=trusted, user_id=h.user_id)
        assert status == 200, (path, status, plain)
        assert hidden_id not in json.dumps(plain).lower(), path
        for hidden_spelling, missing_spelling in zip(_spellings_of(hidden_id), _spellings_of(missing_id), strict=True):
            for name in ("probe", "other", "artifact_id", "limit", "candidate_memory_ids", "metadata_json", "source_refs"):
                asked = {}
                for label, spelling in (("hidden", hidden_spelling), ("missing", missing_spelling)):
                    asked[label] = _call("GET", path, query={name: spelling}, payload=None, key=trusted, user_id=h.user_id)
                # Hidden and missing read alike, whatever the status: the same status and the same body but for the id.
                assert asked["hidden"][0] == asked["missing"][0], (path, name, hidden_spelling)
                status, probed = asked["hidden"]
                if status == 422:
                    # The route declares the parameter and its type refuses the text. The refusal repeats what was sent.
                    refused += 1
                    assert _same_answer(asked["hidden"], hidden_id, asked["missing"], missing_id), (path, name, hidden_spelling)
                    continue
                assert status == 200, (path, name, status)
                assert hidden_id not in json.dumps(probed).lower(), (path, name, hidden_spelling)
                if path == doors[0]:
                    # The report itself reads the same with and without the parameter.
                    assert probed == plain, (path, name, hidden_spelling)
                # Every 200 is the same whole answer for both, but for the events that each read of a trace or an audit writes.
                assert _same_answer(
                    (asked["hidden"][0], _without_events(asked["hidden"][1])), hidden_id,
                    (asked["missing"][0], _without_events(asked["missing"][1])), missing_id,
                ), (path, name, hidden_spelling)
    assert refused, "no probe was refused, so the refusals were not compared"
    # The id was there to be withheld: the owner of the report is shown it.
    status, owner = _call("GET", doors[0], query={}, payload=None, key=admin, user_id=h.user_id)
    assert status == 200 and owner["metadata_json"]["candidate_memory_ids"] == [hidden_id]
    # A POST is held to the same rule. The query of a write is no more a supply than the query of a read.
    status, forgotten = _call(
        "POST", "/v0/vnext/memories/forget", query={"probe": hidden_id},
        payload={"user_id": str(h.user_id), "memory_id": str(shown["id"]), "reason": "r"}, key=trusted, user_id=h.user_id,
    )
    assert status == 200, forgotten
    assert hidden_id not in json.dumps(forgotten).lower()


def _paths_holding(value, needle, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths_holding(child, needle, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from _paths_holding(child, needle, path + "[]")
    elif isinstance(value, str) and needle in value.lower():
        yield path


def test_the_echo_of_a_commit_stands_only_under_the_names_of_its_inputs(label_harness):
    """The answer to a commit repeats what the commit was sent. It does so at the keys of the fields that held the ids.

    A hidden id and a missing id are sent in the same two declared fields, and the answers are the same in shape. The same
    ids sent in no field the answer repeats are not in the answer at all.

    Mutation: the echo covers every key of the answer, as the set of all ids of the request did.
    """

    h = label_harness
    trusted = h.key("trusted_local_agent")
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
    shapes = {}
    for label, row_id in (("hidden", str(hidden["id"])), ("missing", str(uuid4()))):
        status, answer = _call(
            "POST", "/v0/vnext/memories/commit", query={}, key=trusted, user_id=h.user_id,
            payload={
                "user_id": str(h.user_id), "title": "A kiln note", "canonical_text": f"Kiln note {uuid4()}",
                "source_refs": [f"memory:{row_id}"], "contradiction_refs": [row_id], "domain": "project",
                "sensitivity": "public",
            },
        )
        assert status == 201, answer
        paths = sorted(_paths_holding(answer, row_id))
        assert paths, "the answer repeats nothing the caller sent"
        assert all(path.removesuffix("[]").endswith(("source_refs", "contradiction_refs")) for path in paths), paths
        shapes[label] = paths
    assert shapes["hidden"] == shapes["missing"]


def test_a_declared_field_that_names_a_hidden_id_exempts_nothing_in_the_rows_of_the_answer(label_harness):
    """The correction of a readable memory repeats the reason it was sent, and returns the memory with its metadata.

    The reason names an id the key may not read. The metadata of the memory lists the same id as a candidate. The reason
    is the caller's own words, and the candidate list is a stored row: the id of a row the key may not read stands in no
    list of the answer, and it stands in the list for the owner, so the answer had something to withhold.

    Mutation: the ids of an input exempt the id wherever it stands in the answer, as the ids of the whole request did.
    """

    h = label_harness
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
        shown = [_memory(store, f"shown{n}", metadata={"candidate_memory_ids": [str(hidden["id"])]}) for n in range(3)]
    hidden_id = str(hidden["id"])
    stood = {}
    for number, (who, key) in enumerate((("trusted", h.key("trusted_local_agent")), ("admin", h.key("admin_agent")))):
        status, answer = _call(
            "POST", "/v0/vnext/memories/correct", query={}, key=key, user_id=h.user_id,
            payload={
                "user_id": str(h.user_id), "memory_id": str(shown[number]["id"]),
                "canonical_text": "Corrected kiln note", "reason": f"see {hidden_id}",
            },
        )
        assert status == 200, answer
        stood[who] = sorted(_paths_holding(answer, hidden_id))
    assert stood["admin"] == [
        ".memory.metadata_json.agentic_memory.corrections[].reason", ".memory.metadata_json.candidate_memory_ids[]",
    ]
    assert stood["trusted"] == [".memory.metadata_json.agentic_memory.corrections[].reason"]
