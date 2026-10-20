"""The filter that withholds the id of a row a key may not read, through the mounted application on PostgreSQL.

* A POST that carries ``user_id`` only in its JSON body is filtered under ``APP_ENV`` development and test, where nothing
  rewrites the query. The filter reads rows for the user the key was authenticated for, not for the ``user_id`` of the query.
* The workspace task view withholds the id of the report a task made once the report is out of the key's reach.
* A commit that names a hidden id and a commit that names a missing id are answered alike, and read back alike. The id the
  caller sent stays in the answer to the commit where the answer repeats the input that held it, because it is not a row the
  caller discovered.
* An id the caller supplied is no exemption anywhere else: a query parameter the route does not use, or an id in a field
  the answer does not repeat the input at, confirms nothing about a row the key may not read.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode
from uuid import uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_hidden_ids import apply_hidden_id_withholding
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.hidden_ids_postgres_support import normalize


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


def _commit(h, key, refs):
    status, body, _ = h.request(
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


# Whether the answer to the commit shows the id that the form names.
ECHOED = [True, True, True, False, True, None]
# The form that names the id in a field the product does not read as a reference and without a prefix: it may name a
# chunk or a session, and the saved-quote reader holds it to the rule for rows that exist.
INCIDENTAL = 5


def _forms(row_id):
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

    The answer to the commit repeats the ids the caller sent, so a hidden id and a missing id both stand in it. When the
    memory is read back the ids are not the caller's request any more: a hidden id and a missing id are both withheld,
    and the stored memory reads alike, field by field.

    Mutations: ``withhold_json_body`` is given no ``named_body`` (the answer to the commit loses the ids the caller sent);
    ``_Judge.withheld`` asks only whether the id is hidden in a typed position (a missing id stays when the memory is read back).
    """

    h = label_harness
    with h.store() as store:
        hidden = _memory(store, "hidden", sensitivity="confidential")
    hidden_id, missing_id = str(hidden["id"]), str(uuid4())
    trusted = h.key("trusted_local_agent")
    made = {}
    answers = {}
    for label, row_id in (("hidden", hidden_id), ("missing", missing_id)):
        for number, ref in enumerate(_forms(row_id)):
            memory_id, answer = _commit(h, trusted, [ref])
            made[(label, number)] = memory_id
            answers[(label, number)] = answer
            # The caller sent the id, so the answer to the commit shows it, hidden row or not. The exception is a reference
            # under a field the product does not write: a ref drops it with its value, as the saved-quote reader drops it.
            if number != INCIDENTAL:
                assert (row_id in json.dumps(answer).lower()) is ECHOED[number], (label, number)
    for number in range(len(ECHOED)):
        if number != INCIDENTAL:
            assert normalize(answers[("hidden", number)]["memory"]["value"]) == normalize(
                answers[("missing", number)]["memory"]["value"]
            )
    for number in range(len(ECHOED)):
        hidden_view = _read_back(h, monkeypatch, trusted, made[("hidden", number)])
        missing_view = _read_back(h, monkeypatch, trusted, made[("missing", number)])
        assert hidden_id not in json.dumps(hidden_view).lower()
        if number == INCIDENTAL:
            # An id under a field the product does not read as a reference may name a chunk or a session. The saved-quote
            # reader refuses it only when it names a stored row, and then it removes the copy of the quote beside it, and the
            # filter withholds only the id of a stored row there too. That is the boundary the security note states. The id of
            # the hidden row is gone, and the id of a missing row stands as the caller wrote it.
            continue
        assert missing_id not in json.dumps(missing_view).lower()
        # Every id and instant is replaced before two answers are compared, so they compare by what they say.
        for door in ("review", "explain"):
            assert normalize(hidden_view[door]) == normalize(missing_view[door]), (number, door)
        assert normalize(hidden_view["audit"]["memory"]["value"]) == normalize(missing_view["audit"]["memory"]["value"])
        assert normalize(hidden_view["audit"]["memory"]["metadata_json"]) == normalize(
            missing_view["audit"]["memory"]["metadata_json"]
        )


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


def _spellings_of(row_id):
    return [row_id, row_id.upper(), row_id.replace("-", ""), f"memory:{row_id}", f"urn:uuid:{row_id}"]


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
    for path in doors:
        status, plain = _call("GET", path, query={}, payload=None, key=trusted, user_id=h.user_id)
        assert status == 200, (path, status, plain)
        assert hidden_id not in json.dumps(plain).lower(), path
        for spelling in _spellings_of(hidden_id):
            for name in ("probe", "other", "artifact_id", "limit", "candidate_memory_ids", "metadata_json", "source_refs"):
                status, probed = _call("GET", path, query={name: spelling}, payload=None, key=trusted, user_id=h.user_id)
                if status == 422:
                    continue  # the route declares the parameter and its type refuses the text
                assert status == 200, (path, name, status)
                assert hidden_id not in json.dumps(probed).lower(), (path, name, spelling)
                if path == doors[0]:
                    # The report itself reads the same with and without the parameter. (A trace or an audit lists the
                    # policy events that each read writes, so its events differ by one on every call.)
                    assert probed == plain, (path, name, spelling)
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
