"""The rows that keep refs a writer chose hold them to the caller's fence at every door of the mounted application.

Unreleased (on main, not in v0.20.0). The saved-quote reader withholds the words of a memory or a source the caller may not read
from the ref that cites it. Four kinds of row keep a structure their writer chose, and each is reached by a caller with limits
after the memory it cites is redacted, so each is held to the same rule:

* a source made by the agent-output ingest keeps the ``source_refs`` it was sent, in ``metadata_json`` and again in
  ``metadata_json.raw_payload``; the routes that return a source return them (the source, its review and delete answers, its
  trace, the trace of an artifact made from it, the context pack, the workspace and the status of the connector);
* the metadata of a quality rating (the artifact trace, the rating list, the workspace);
* the allowed sources and the scope of a queued task (the workspace lists the task as stored), and the artifact the worker
  makes from it, which prints both (it is derived from the rows the task names, so a redaction contains it);
* the report a consolidation run makes (``tests/integration/test_consolidation_report_sensitivity_postgres.py``).

The vault of the operator route sweep with ``redacted_family=True`` builds all of them before the memory is redacted, and
``tests/integration/test_saved_quote_memory_refs_postgres.py`` reads every route with it. These tests read the same vault door
by door and require what that sweep cannot: that the row is shown to a key that may read it, with its ids and the marker of a
withheld quote, and that the owner and an unbound admin key are shown it as it was stored. A key that cannot see the row at all
would pass the sweep, so each test names the door that must show it.

The command line is the last door: ``alicebot vnext memories audit`` with an agent key was handed the audit as stored.

Each test names the mutation that must fail it, and ``scripts/derived_label_mutations.json`` replays it.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import ALL_SENSITIVITY, Call
from tests.integration.operator_route_runner import run_call
from tests.integration.operator_route_vault import Vault
from tests.integration.test_saved_quote_memory_refs_postgres import _keys


def _vault(label_harness) -> Vault:
    return Vault(label_harness, "r", redacted_family=True).build()


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _expected(cited: str) -> list[object]:
    """The refs the ingest source was sent, as a caller who may not read the memory is shown them."""

    return [{"memory_id": cited, "quote": None}, f"memory:{cited}", {"quote": None}]


def _stored(cited: str, words: str) -> list[object]:
    return [{"memory_id": cited, "quote": words, words: True}, f"memory:{cited}", {"quote": words}]


def _source_rows(vault: Vault, key: str | None, source_id: str, artifact_id: str) -> dict[str, dict]:
    """The row of the ingest source at every door that returns a source, as one key is shown it. A door that answers an error for
    the key is left out."""

    pack = {"query": "quotes redacted", "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "limit": 50}}
    calls = {
        "source": ("GET", "/v0/vnext/sources/{source_id}", Call("source", path={"source_id": source_id})),
        "source trace": ("GET", "/v0/vnext/traces/sources/{source_id}", Call("trace", path={"source_id": source_id})),
        "artifact trace": ("GET", "/v0/vnext/traces/artifacts/{artifact_id}", Call("trace", path={"artifact_id": artifact_id})),
        "context pack": ("POST", "/v0/vnext/context-packs", Call("pack", body=pack)),
        "workspace": ("GET", "/v0/vnext/workspace", Call("workspace")),
        "connector status": ("GET", "/v0/vnext/connectors/{connector_name}/status", Call("status", path={"connector_name": "agent_output"})),
        "review": (
            "POST", "/v0/vnext/sources/{source_id}/review",
            Call("review", path={"source_id": source_id}, body={"action": "review"}, mutates=True),
        ),
    }
    found: dict[str, dict] = {}
    for door, (method, template, call) in calls.items():
        status, text = run_call(vault, method, template, call, key)
        if status not in {200, 201}:
            continue
        body = _json(text)
        row = _pick_source(door, body, source_id)
        if row is not None:
            found[door] = row
        if door == "workspace" and body:
            traced = [
                item["source"] for item in body["traceability"]["items"] if str(item["source"].get("id")) == source_id
            ]
            if traced:
                found["workspace trace"] = traced[0]
    return found


def _pick_source(door: str, body: object, source_id: str) -> dict | None:
    if not isinstance(body, dict):
        return None
    if door == "source":
        return body
    if door in {"source trace"}:
        return body.get("source")
    if door == "review":
        return body.get("source")
    listed = {
        "artifact trace": body.get("sources"),
        "context pack": body.get("sources"),
        "workspace": body.get("sources"),
        "connector status": body.get("recent_captures"),
    }.get(door)
    for row in listed or []:
        if str(row.get("id")) == source_id:
            return row
    return None


def test_every_door_that_returns_a_source_withholds_the_words_of_a_redacted_memory_from_a_key_that_may_not_read_it(label_harness):
    """A source made by the agent-output ingest cites a memory with ``{"memory_id": "<id>", "quote": "..."}``, with the same words
    as the name of a field, with ``memory:<id>`` and with a quote beside it. The memory is redacted. An unbound trusted key and a
    key that only reaches the trace and the context pack (read-only, memory-proposal) are shown the source with the ids and the
    marker of a withheld quote in both copies of the refs, and none of the words; the owner's vault and an unbound admin key
    are shown what was stored. The doors a key reaches differ, and each key must reach the ones named here.

    Mutations, each alone, in the routers and ``vnext_retrieval.py``: delete the ``_vnext_source_for_caller`` call from
    ``get_vnext_source``; from ``_vnext_load_source_trace``; from the ``archived`` answer of ``review_vnext_source`` and from its
    ``updated`` answer; delete the ``sources`` call from the workspace; delete ``_vnext_sources_for_caller`` from the artifact
    trace; delete the ``sources`` call from the context pack; delete it from the connector status. Each is in the manifest.
    """

    vault = _vault(label_harness)
    keys = _keys(vault, label_harness)
    cited = vault.ids["memory_redacted"]
    words = vault.text("memory_redacted-text")
    source_id = vault.ids["ingest_quotes_redacted"]
    artifact_id = vault.ids["ingest_quotes_redacted_artifact"]
    reached = {
        "trusted": {"source", "source trace", "artifact trace", "context pack", "workspace", "workspace trace", "connector status", "review"},
        "read_only": {"artifact trace", "context pack"},
        "memory_proposal": {"artifact trace", "context pack"},
    }
    for profile, doors in reached.items():
        shown = _source_rows(vault, keys[profile], source_id, artifact_id)
        assert doors <= set(shown), (profile, sorted(set(shown)), sorted(doors - set(shown)))
        for door, row in shown.items():
            assert words not in json.dumps(row), (profile, door)
            assert row["metadata_json"]["source_refs"] == _expected(cited), (profile, door)
            assert row["metadata_json"]["raw_payload"]["source_refs"] == _expected(cited), (profile, door)
            assert row["title"] == vault.text("ingest_quotes_redacted-title"), (profile, door, "the rest of the row is untouched")
            assert row["metadata_json"]["raw_payload"]["content"] == vault.text("ingest_quotes_redacted-text"), (profile, door)
    for profile in ("trusted_bound", "alpha_only", "admin_bound"):
        for door, row in _source_rows(vault, keys[profile], source_id, artifact_id).items():
            assert words not in json.dumps(row), (profile, door)
    admin = _source_rows(vault, vault.keys["admin"], source_id, artifact_id)
    assert {"source", "source trace", "artifact trace", "context pack", "workspace", "connector status"} <= set(admin), sorted(admin)
    for door, row in admin.items():
        assert row["metadata_json"]["source_refs"] == _stored(cited, words), door
        assert row["metadata_json"]["raw_payload"]["source_refs"] == _stored(cited, words), door


def test_the_delete_answer_of_a_source_withholds_the_words_and_the_stored_row_keeps_them(label_harness):
    """The answer of ``DELETE /v0/vnext/sources/{id}`` returns the source row. An unbound trusted key is shown the refs without
    the words, and the row in the store is not changed by the read, so the owner reads the refs as they were stored afterwards.

    Mutation: delete the ``_vnext_source_for_caller`` call from ``delete_vnext_source``.
    """

    vault = _vault(label_harness)
    cited = vault.ids["memory_redacted"]
    words = vault.text("memory_redacted-text")
    source_id = vault.ids["ingest_quotes_redacted"]
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT metadata_json FROM sources WHERE id = %s", (source_id,))
        stored = cur.fetchone()["metadata_json"]
    assert stored["source_refs"] == _stored(cited, words)
    status, text = run_call(vault, "DELETE", "/v0/vnext/sources/{source_id}", Call("delete", path={"source_id": source_id}, mutates=True), vault.keys["trusted"])
    assert status == 200, text
    row = json.loads(text)
    assert words not in text and row["metadata_json"]["source_refs"] == _expected(cited)
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT metadata_json FROM sources WHERE id = %s", (source_id,))
        after = cur.fetchone()["metadata_json"]
    assert after["source_refs"] == _stored(cited, words) and after["raw_payload"]["source_refs"] == _stored(cited, words)


def test_the_archive_answer_of_a_source_withholds_the_words_and_the_trace_inside_it_too(label_harness):
    """``POST /v0/vnext/sources/{id}/review`` with ``archive`` returns the archived row and the trace of it. An unbound trusted key is
    shown the row and the trace without the words, in both copies of the refs.

    Mutation: delete the ``_vnext_source_for_caller`` call from the ``archived`` answer of ``review_vnext_source``.
    """

    vault = _vault(label_harness)
    cited = vault.ids["memory_redacted"]
    words = vault.text("memory_redacted-text")
    source_id = vault.ids["ingest_quotes_redacted"]
    status, text = run_call(
        vault, "POST", "/v0/vnext/sources/{source_id}/review",
        Call("archive", path={"source_id": source_id}, body={"action": "archive"}, mutates=True), vault.keys["trusted"],
    )
    assert status == 200, text
    body = json.loads(text)
    assert body["archived"] is True and words not in text
    assert body["source"]["metadata_json"]["source_refs"] == _expected(cited)
    assert body["source"]["metadata_json"]["raw_payload"]["source_refs"] == _expected(cited)
    assert body["trace"]["source"]["metadata_json"]["source_refs"] == _expected(cited)


def test_a_source_that_cites_a_source_raised_above_the_ceiling_loses_the_entry(label_harness):
    """The refs of a source made by an agent can cite another source, and that source can be raised afterwards. The entry that
    names it is dropped from both copies for a key that may no longer read it, and an entry that names a readable source stays.

    Mutation: pass an empty set of refused sources in ``SavedProvenanceReader._shown_refs``.
    """

    vault = _vault(label_harness)
    readable = vault.ids["source_shown"]
    hidden = vault.ids["source_hidden"]
    refs = [{"source_id": readable, "quote": "readable words"}, {"source_id": hidden, "quote": "hidden words"}, f"source:{hidden}"]
    status, body = vault.admin_request(
        "POST",
        "/v0/vnext/agents/ingest-output",
        {
            "agent_id": vault.agent_ids.get("admin_agent", "sweep-ingest"),
            "title": vault.shown("cites_a_source-title"),
            "content": vault.shown("cites_a_source-text"),
            "domain": "project",
            "sensitivity": "public",
            "source_refs": refs,
        },
    )
    assert status in {200, 201}, (status, str(body)[:300])
    cites = body["source_id"]
    status, text = run_call(vault, "GET", "/v0/vnext/sources/{source_id}", Call("source", path={"source_id": cites}), vault.keys["trusted"])
    assert status == 200, text
    row = json.loads(text)
    expected = [{"source_id": readable, "quote": "readable words"}]
    assert row["metadata_json"]["source_refs"] == expected
    assert row["metadata_json"]["raw_payload"]["source_refs"] == expected
    assert "hidden words" not in text
    status, text = run_call(vault, "GET", "/v0/vnext/sources/{source_id}", Call("source", path={"source_id": cites}), vault.keys["admin"])
    assert json.loads(text)["metadata_json"]["source_refs"] == refs


def test_the_workspace_and_the_rating_routes_hold_a_task_and_a_rating_to_the_reader(label_harness):
    """The workspace lists the newest tasks as stored, and the quality ratings of the artifacts; the artifact trace lists the
    ratings of one artifact and ``GET /v0/vnext/quality-evals`` lists them all. A task whose allowed sources quote the redacted
    memory, and whose scope writes the words as a field name, and a rating whose metadata does the same, are shown to an unbound
    trusted key with the ids and the marker of a withheld quote. The key that reaches only the artifact trace (read-only,
    memory-proposal) is shown the rating the same way. The unbound admin key is shown both as they were stored.

    Mutations, each alone: delete the ``fields`` call for the tasks from the workspace; delete it for the ratings; delete
    ``_vnext_ratings_for_caller`` from the artifact trace and from ``list_vnext_quality_evals``.
    """

    vault = _vault(label_harness)
    keys = _keys(vault, label_harness)
    cited = vault.ids["memory_redacted"]
    words = vault.text("memory_redacted-text")
    task_id, rating_id = vault.ids["task_quotes_redacted"], vault.ids["rating_quotes_redacted"]
    artifact_id = vault.ids["artifact_shown"]

    def ratings(key: str | None) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for door, (method, template, call) in {
            "artifact trace": ("GET", "/v0/vnext/traces/artifacts/{artifact_id}", Call("trace", path={"artifact_id": artifact_id})),
            "quality evals": ("GET", "/v0/vnext/quality-evals", Call("evals", query={"artifact_id": artifact_id})),
            "workspace": ("GET", "/v0/vnext/workspace", Call("workspace")),
        }.items():
            status, text = run_call(vault, method, template, call, key)
            if status != 200:
                continue
            body = json.loads(text)
            rows = {"artifact trace": body.get("quality_evals"), "quality evals": body.get("items"), "workspace": body.get("quality_evals")}[door]
            row = next((item for item in rows or [] if str(item.get("id")) == rating_id), None)
            if row is not None:
                found[door] = row
        return found

    for profile in ("trusted", "read_only", "memory_proposal"):
        shown = ratings(keys[profile])
        assert "artifact trace" in shown, (profile, sorted(shown))
        if profile == "trusted":
            assert {"artifact trace", "quality evals", "workspace"} <= set(shown), sorted(shown)
        for door, row in shown.items():
            assert row["metadata_json"] == {"memory_id": cited, "quote": None}, (profile, door)
            assert words not in json.dumps(row), (profile, door)
    for door, row in ratings(vault.keys["admin"]).items():
        assert row["metadata_json"]["quote"] == words and row["metadata_json"][words] == 1, door
        assert row["metadata_json"]["artifact_type"] == "daily_brief", door
    status, text = run_call(vault, "GET", "/v0/vnext/workspace", Call("workspace"), keys["trusted"])
    task = next(item for item in json.loads(text)["tasks"] if str(item["id"]) == task_id)
    assert task["allowed_sources_json"] == [{"memory_id": cited, "quote": None}]
    assert task["scope_json"] == {}
    assert task["title"] == vault.text("task_quotes_redacted-title") and words not in json.dumps(task)
    status, text = run_call(vault, "GET", "/v0/vnext/workspace", Call("workspace"), vault.keys["admin"])
    task = next(item for item in json.loads(text)["tasks"] if str(item["id"]) == task_id)
    assert task["allowed_sources_json"] == [{"memory_id": cited, "quote": words, words: True}]
    assert task["scope_json"] == {words: cited}


def test_the_artifact_a_queued_task_made_is_contained_with_the_memory_it_names(label_harness):
    """The worker prints the scope and the allowed sources of a task into the artifact it makes, with no record of the memory
    they name, so redacting the memory left the artifact readable with the words in it. The artifact is derived from the rows the
    task names, so a key with a ceiling is told there is no such artifact once the memory is redacted, and the artifact list does
    not carry it. The unbound admin key reads the artifact with the words.

    Mutation: replace ``_task_artifact_metadata`` with a function that returns the metadata it was given.
    """

    vault = _vault(label_harness)
    keys = _keys(vault, label_harness)
    words = vault.text("memory_redacted-text")
    artifact_id = vault.ids["task_quotes_redacted_artifact"]
    cited = vault.ids["memory_redacted"]
    for profile in ("trusted", "read_only", "memory_proposal", "trusted_bound"):
        for template in ("/v0/vnext/artifacts/{artifact_id}", "/v0/vnext/traces/artifacts/{artifact_id}"):
            status, text = run_call(vault, "GET", template, Call("artifact", path={"artifact_id": artifact_id}), keys[profile])
            assert status in {403, 404} and words not in text, (profile, template, status)
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts", Call("list"), keys["trusted"])
    assert status == 200 and artifact_id not in text and words not in text
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": artifact_id}), vault.keys["admin"])
    assert status == 200 and words in text
    assert cited in json.dumps(json.loads(text)["metadata_json"]["derived_from"])


def test_a_task_that_names_a_memory_already_redacted_is_processed_and_its_artifact_is_contained(label_harness):
    """A task queued after the memory was redacted still runs: the worker reads the row of a redacted memory to derive the
    artifact from it, and a key with a ceiling is told there is no such artifact.

    Mutation: read the memories a task names without the soft-deleted rows (``memory_rows_including_deleted`` replaced by a live
    read in ``_rows_named_by_task``): the artifact is made with no input and a trusted key reads the words.
    """

    vault = _vault(label_harness)
    words = vault.text("memory_redacted-text")
    cited = vault.ids["memory_redacted"]
    status, body = vault.admin_request(
        "POST",
        "/v0/vnext/queue/tasks",
        {
            "title": "Late task",
            "task_type": "summarize",
            "instructions": "Summarize",
            "domain": "project",
            "sensitivity": "public",
            "allowed_sources_json": [{"memory_id": cited, "quote": words}],
        },
    )
    assert status in {200, 201}, (status, str(body)[:300])
    task_id = str((body.get("task") or body)["id"])
    # The vault queued two tasks of its own that nobody processed, and the worker takes the oldest first.
    for _ in range(4):
        status, body = vault.admin_request("POST", "/v0/vnext/queue/process-next", {})
        assert status == 200 and body["status"] == "completed", body
        if body["task_id"] == task_id:
            break
    assert body["task_id"] == task_id
    artifact_id = body["artifact_id"]
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": artifact_id}), vault.keys["trusted"])
    assert status == 404 and words not in text
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": artifact_id}), vault.keys["admin"])
    assert status == 200 and words in text


def test_the_artifact_of_a_task_that_names_a_confidential_source_is_contained(label_harness):
    """The sources a task may use are inputs of its artifact as the memories are. A task over a public source that is then raised to
    confidential makes an artifact an unbound trusted key cannot read; the label of the task alone did not cover the source.

    Mutation: leave the sources out of ``_rows_named_by_task``.
    """

    vault = _vault(label_harness)
    hidden = vault.ids["source_hidden"]
    status, body = vault.admin_request(
        "POST",
        "/v0/vnext/queue/tasks",
        {
            "title": "Source task",
            "task_type": "summarize",
            "instructions": "Summarize",
            "domain": "project",
            "sensitivity": "public",
            "allowed_sources_json": [{"source_id": hidden, "quote": "the hidden words"}],
        },
    )
    assert status in {200, 201}, (status, str(body)[:300])
    task_id = str((body.get("task") or body)["id"])
    for _ in range(5):
        status, body = vault.admin_request("POST", "/v0/vnext/queue/process-next", {})
        assert status == 200 and body["status"] == "completed", body
        if body["task_id"] == task_id:
            break
    assert body["task_id"] == task_id
    artifact_id = body["artifact_id"]
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": artifact_id}), vault.keys["trusted"])
    assert status == 404 and "the hidden words" not in text
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": artifact_id}), vault.keys["admin"])
    assert status == 200 and "the hidden words" in text


def test_a_task_that_names_no_memory_or_source_makes_an_artifact_with_no_record_of_inputs(label_harness):
    """The control: a task with neither scope nor allowed sources makes the artifact it always made, with the metadata it had, so
    a vault that queues plain tasks is unchanged.

    Mutation: stamp ``derived_from`` on every artifact the worker makes (drop the early return of ``_task_artifact_metadata``).
    """

    vault = _vault(label_harness)
    status, body = vault.admin_request(
        "POST",
        "/v0/vnext/queue/tasks",
        {"title": "Plain task", "task_type": "summarize", "instructions": "Summarize", "domain": "project", "sensitivity": "public"},
    )
    assert status in {200, 201}, (status, str(body)[:300])
    task_id = str((body.get("task") or body)["id"])
    for _ in range(4):
        status, body = vault.admin_request("POST", "/v0/vnext/queue/process-next", {})
        assert status == 200 and body["status"] == "completed", body
        if body["task_id"] == task_id:
            break
    assert body["task_id"] == task_id
    status, text = run_call(vault, "GET", "/v0/vnext/artifacts/{artifact_id}", Call("artifact", path={"artifact_id": body["artifact_id"]}), vault.keys["admin"])
    metadata = json.loads(text)["metadata_json"]
    assert set(metadata) == {"task_id", "task_type"}, metadata


# -- the command line --------------------------------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    from alicebot_api.cli import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def test_the_command_line_audit_holds_a_key_to_the_fence_of_the_audit_route(label_harness, monkeypatch):
    """``alicebot vnext memories audit`` with ``ALICE_AGENT_API_KEY`` and a matching ``--agent-id`` ran the action-level policy
    check and returned the audit as stored: the full row of a memory above the key's ceiling, a commit made from a redacted
    memory, and the saved quote of a redacted memory. It now authorizes the root and every memory of the replacement chain as the
    route does (a memory the caller may not read answers as one that does not exist), and holds the envelope to the saved-quote
    reader. A read-only key is shown the commit that quoted the memory without the words. The owner, and an unbound admin key,
    are shown the audit as stored. ``vnext memories recent`` already did this.

    Mutations, each alone, in ``_run_vnext_memory_audit``: pass no ``authorize_memory``; skip the reader.
    """

    vault = _vault(label_harness)
    words = vault.text("memory_redacted-text")
    uid = str(label_harness.user_id)
    read_only_key = label_harness.key("read_only_agent")
    with label_harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT agent_id, permission_profile FROM agent_api_keys")
        agents = {row["permission_profile"]: row["agent_id"] for row in cur.fetchall()}
    saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "DATABASE_ADMIN_URL", "ALICE_AGENT_API_KEY")}
    os.environ["DATABASE_URL"] = label_harness.urls["app"]
    os.environ["DATABASE_ADMIN_URL"] = label_harness.urls["admin"]
    try:
        def audit(memory_id: str, *, profile: str | None = None, key: str | None = None) -> tuple[int, str]:
            argv = ["--user-id", uid, "vnext", "memories", "audit", memory_id]
            if profile is not None:
                argv += ["--agent-id", agents[profile], "--permission-profile", profile]
            if key is None:
                monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
            else:
                monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
            code, out, _err = _run_cli(argv)
            return code, out

        quoting = vault.ids["commit_confirmed_quotes_redacted"]
        # The control: the owner and an unbound admin key are shown the words.
        code, out = audit(quoting)
        assert code == 0 and words in out, "the owner reads the audit as stored"
        code, out = audit(quoting, profile="admin_agent", key=vault.keys["admin"])
        assert code == 0 and words in out, "an unbound admin key reads the audit as stored"
        # A key with limits is shown the commit, and none of the words.
        code, out = audit(quoting, profile="read_only_agent", key=read_only_key)
        assert code == 0 and words not in out and json.loads(out)["memory"]["id"] == quoting
        # A memory the key may not read answers as one that does not exist, and so does a commit made from a redacted memory.
        for name in ("memory_hidden", "commit_of_redacted", "memory_redacted"):
            code, out = audit(vault.ids[name], profile="read_only_agent", key=read_only_key)
            assert code != 0 and vault.text(f"{name}-text") not in out, name
        code, out = audit(vault.ids["memory_hidden"], profile="trusted_local_agent", key=vault.keys["trusted"])
        assert code != 0 and vault.text("memory_hidden-text") not in out
        # A call that declares a profile with no key is held to it, as recent commits holds it.
        code, out = audit(quoting, profile="read_only_agent")
        assert code == 0 and words not in out
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
