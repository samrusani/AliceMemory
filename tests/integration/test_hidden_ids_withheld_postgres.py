"""The filter that withholds the id of a row a key may not read, through the mounted application on PostgreSQL.

* A POST that carries ``user_id`` only in its JSON body is filtered under ``APP_ENV`` development and test, where nothing
  rewrites the query. The filter reads rows for the user the key was authenticated for, not for the ``user_id`` of the query.
* The workspace task view withholds the id of the report a task made once the report is out of the key's reach.
* A commit that names a hidden id and a commit that names a missing id are answered alike, and read back alike. The id the
  caller sent stays in the answer to the commit, because it is not a row the caller discovered.
"""

from __future__ import annotations

import json
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
