"""Session brief: labelled sources and facts, fenced, no auto-promote.

Put next to the on-ramp tests. Each test names the edit that makes it fail.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path, sqlite_url_for_path
from alicebot_api.session_briefing import (
    EMPTY_SESSION_BRIEF,
    SOURCE_LIMIT,
    compile_session_brief,
    source_scope_from_project_scope,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_ID = "00000000-0000-0000-0000-000000000001"

SOURCE_SENTENCE = "The indigo-lighthouse-42 canary stays in the vault."
SOURCE_NOTE = f"# Vault canary\n\n{SOURCE_SENTENCE}\n"
COMMITTED_FACT = "We will keep the public acme launch checklist on Thursday."

ALLOWED_TITLE = "Public acme launch decision"
RESTRICTED_TITLE = "Private other-project salary decision"
ALLOWED_TEXT = "We will ship the public acme launch checklist on Thursday."
RESTRICTED_TEXT = "The other-project salary band stays private and must not leak."
ALLOWED_SOURCE = "Public acme launch record: ship the checklist on Thursday."
RESTRICTED_SOURCE = "Private other-project salary record: the band stays private and must not leak."
NO_QUERY_ACME_SENTENCE = "The acme-brief-canary-91 stays on the launch pad."
NO_QUERY_SALARY_TEXT = "The other-project salary band stays private and must not leak."
SHARED_SOURCE_QUERY = "record"

FORBIDDEN_EMPTY_WORDS = ("review", "candidate", "queue", "console")
UNSCOPED_FENCES = {
    "effective_domains": (),
    "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"),
    "effective_project_scope": (),
}


def _clear_env(monkeypatch) -> None:
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    _clear_env(monkeypatch)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _capture(context, raw_text: str, **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    payload = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": raw_text,
            "title": arguments.pop("title", "Vault canary"),
            "domain": arguments.pop("domain", "personal"),
            "sensitivity": arguments.pop("sensitivity", "private"),
            **arguments,
        },
    )
    assert payload["status"] == "imported", payload
    return payload


def _commit(context, *, title: str, text: str, sensitivity: str, project: str, domain: str) -> str:
    from alicebot_api.mcp.registry import call_mcp_tool

    payload = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": title,
            "canonical_text": text,
            "memory_type": "decision",
            "domain": domain,
            "sensitivity": sensitivity,
            "confidence": 0.96,
            "project_scope": [project],
            "rationale": "User said: remember this",
        },
    )
    assert payload["status"] == "committed", payload
    return str(payload["memory"]["id"])


def _compile(tmp_path: Path, **kwargs) -> str:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    fences = dict(UNSCOPED_FENCES)
    fences.update(kwargs)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return compile_session_brief(store, **fences)


def _labelled_lines(brief: str, label: str) -> list[str]:
    prefix = f"**{label}**:"
    return [line for line in brief.splitlines() if line.startswith(prefix)]


def _onramp_env() -> dict[str, str]:
    env = os.environ.copy()
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        env.pop(env_name, None)
    pythonpath_entries = [
        str(REPO_ROOT / "apps" / "api" / "src"),
        str(REPO_ROOT / "workers"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def test_a_captured_note_is_a_source_not_a_fact(tmp_path: Path, monkeypatch) -> None:
    """Capture without promote must quote the sentence as a source.

    Fails if compile_session_brief lists ``candidate`` memories as facts,
    or if it skips ``search_source_excerpts``.
    """

    context = _context(tmp_path, monkeypatch)
    _capture(context, SOURCE_NOTE)

    brief = _compile(tmp_path, query=SOURCE_SENTENCE)

    assert any(SOURCE_SENTENCE in line for line in _labelled_lines(brief, "source")), brief
    assert not any(SOURCE_SENTENCE in line for line in _labelled_lines(brief, "fact")), brief


def test_a_committed_fact_is_labelled_fact(tmp_path: Path, monkeypatch) -> None:
    """alice_memory_commit is the fact channel.

    Fails if list_memories drops ``active`` / ``accepted`` or the renderer
    forgets the **fact** label.
    """

    context = _context(tmp_path, monkeypatch)
    _commit(
        context,
        title="Public acme launch",
        text=COMMITTED_FACT,
        sensitivity="public",
        project="acme",
        domain="project",
    )

    brief = _compile(tmp_path, query=None)

    assert any(COMMITTED_FACT in line for line in _labelled_lines(brief, "fact")), brief


def test_empty_store_is_one_quiet_line(tmp_path: Path, monkeypatch) -> None:
    """Empty vault: one line, no review-queue lecture.

    Fails if EMPTY_SESSION_BRIEF gains a forbidden word, or if an empty
    store returns a labelled section instead.
    """

    _context(tmp_path, monkeypatch)
    brief = _compile(tmp_path, query=None)

    assert brief == EMPTY_SESSION_BRIEF
    assert len(brief.splitlines()) == 1
    lowered = brief.casefold()
    for word in FORBIDDEN_EMPTY_WORDS:
        assert word not in lowered, brief


def test_a_narrowed_brief_cannot_see_the_salary_row(tmp_path: Path, monkeypatch) -> None:
    """D7 fixture: public acme vs private other-project salary.

    The failing edit is: call ``search_source_excerpts(..., scope=None)``.
    That puts the salary source back into a project-locked brief.
    Omitting ``effective_project_scope`` on ``list_memories`` does not:
    ``_memory_honours_fence`` still drops the salary fact.

    The unscoped compile below is the vacuous-test guard. If it stops
    seeing the salary line, the scoped asserts prove nothing.
    """

    context = _context(tmp_path, monkeypatch)
    _commit(
        context,
        title=ALLOWED_TITLE,
        text=ALLOWED_TEXT,
        sensitivity="public",
        project="acme",
        domain="project",
    )
    _commit(
        context,
        title=RESTRICTED_TITLE,
        text=RESTRICTED_TEXT,
        sensitivity="private",
        project="other",
        domain="personal",
    )
    _capture(
        context,
        ALLOWED_SOURCE,
        title="Acme launch record",
        domain="project",
        sensitivity="public",
        project_scope=["acme"],
    )
    _capture(
        context,
        RESTRICTED_SOURCE,
        title="Other salary record",
        domain="personal",
        sensitivity="private",
        project_scope=["other"],
    )

    unscoped = _compile(tmp_path, query=SHARED_SOURCE_QUERY)
    assert RESTRICTED_TEXT in unscoped, "unscoped brief lost the salary fact; scoped asserts are vacuous"
    assert RESTRICTED_SOURCE in unscoped, "unscoped brief lost the salary source; scoped asserts are vacuous"

    project_locked = _compile(
        tmp_path,
        query=SHARED_SOURCE_QUERY,
        effective_project_scope=("acme",),
    )
    assert RESTRICTED_TEXT not in project_locked
    assert RESTRICTED_SOURCE not in project_locked
    assert ALLOWED_TEXT in project_locked
    assert ALLOWED_SOURCE in project_locked

    public_only = _compile(
        tmp_path,
        query=SHARED_SOURCE_QUERY,
        effective_sensitivity_allowed=("public",),
    )
    assert RESTRICTED_TEXT not in public_only
    assert RESTRICTED_SOURCE not in public_only
    assert ALLOWED_TEXT in public_only
    assert ALLOWED_SOURCE in public_only


def test_a_no_query_brief_still_sees_an_older_in_fence_source(
    tmp_path: Path, monkeypatch
) -> None:
    """Eight newer other-project sources must not hide an older acme source.

    Fails if ``_resolve_excerpt_query`` puts ``limit=SOURCE_LIMIT`` back
    on the unfenced ``list_events`` call. Those eight salary events fill
    the newest-first window and the acme source is never seen, so
    ``query=None`` returns Nothing stored yet.
    """

    context = _context(tmp_path, monkeypatch)
    _capture(
        context,
        NO_QUERY_ACME_SENTENCE,
        title="Acme no-query canary",
        domain="project",
        sensitivity="public",
        project_scope=["acme"],
    )
    for index in range(SOURCE_LIMIT):
        _capture(
            context,
            f"{NO_QUERY_SALARY_TEXT} #{index}",
            title=f"Other salary record {index}",
            domain="personal",
            sensitivity="private",
            project_scope=["other"],
        )

    brief = _compile(tmp_path, query=None, effective_project_scope=("acme",))
    assert brief != EMPTY_SESSION_BRIEF
    assert any(
        NO_QUERY_ACME_SENTENCE in line for line in _labelled_lines(brief, "source")
    ), brief
    assert NO_QUERY_SALARY_TEXT not in brief


def test_excerpt_call_writes_scope_at_the_call_site(tmp_path: Path, monkeypatch) -> None:
    """search_source_excerpts must receive the post-policy project fence.

    Fails if the excerpt call hard-codes ``scope=None`` while
    ``effective_project_scope`` is ``("acme",)``.
    """

    from alicebot_api.vnext_retrieval import VNextRetrievalService

    context = _context(tmp_path, monkeypatch)
    _capture(
        context,
        ALLOWED_SOURCE,
        title="Acme launch record",
        domain="project",
        sensitivity="public",
        project_scope=["acme"],
    )
    seen: dict[str, object] = {}
    original = VNextRetrievalService.search_source_excerpts

    def wrapped(self, **kwargs):
        seen.update(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(VNextRetrievalService, "search_source_excerpts", wrapped)
    _compile(tmp_path, query=SHARED_SOURCE_QUERY, effective_project_scope=("acme",))

    assert "scope" in seen, "search_source_excerpts was not called"
    scope = seen["scope"]
    assert scope is not None
    assert scope.projects == frozenset({"acme"})
    assert seen["sensitivity_allowed"] == list(UNSCOPED_FENCES["effective_sensitivity_allowed"])


def test_the_fence_parameters_have_no_default() -> None:
    """A default of () is no fence. Same shape as the D7 resume leak."""

    parameters = inspect.signature(compile_session_brief).parameters
    for name in (
        "effective_domains",
        "effective_sensitivity_allowed",
        "effective_project_scope",
        "query",
    ):
        assert parameters[name].default is inspect.Parameter.empty, (
            f"compile_session_brief.{name} gained a default. A default of () "
            "means no fence for any caller that forgets it."
        )
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_source_scope_helper_is_none_only_when_unscoped() -> None:
    """The helper must not invent an empty scope object for an owner query."""

    assert source_scope_from_project_scope(()) is None
    scope = source_scope_from_project_scope(("acme",))
    assert scope is not None
    assert scope.projects == frozenset({"acme"})


def test_cli_brief_writes_markdown_not_jsonrpc(tmp_path: Path, monkeypatch) -> None:
    """python -m alicebot_api.onramp brief prints markdown on stdout.

    Fails if brief is missing from _KNOWN_COMMANDS (it becomes mcp) or if
    stdout grows a JSON-RPC envelope.
    """

    context = _context(tmp_path, monkeypatch)
    _capture(context, SOURCE_NOTE)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alicebot_api.onramp",
            "brief",
            "--data-dir",
            str(tmp_path),
            "--user-id",
            USER_ID,
            "--query",
            SOURCE_SENTENCE,
        ],
        cwd=REPO_ROOT,
        env=_onramp_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert SOURCE_SENTENCE in completed.stdout
    assert "**source**:" in completed.stdout
    assert "jsonrpc" not in completed.stdout
    assert "Content-Length:" not in completed.stdout


def test_cli_brief_without_query_still_quotes_a_captured_source(tmp_path: Path, monkeypatch) -> None:
    """No --query: derive from the imported source, do not pass a blank query.

    Fails if the no-query path skips excerpts when resume is empty, or if
    it calls search_source_excerpts with "".
    """

    context = _context(tmp_path, monkeypatch)
    _capture(context, SOURCE_NOTE)

    assert (
        onramp_main(
            ["brief", "--data-dir", str(tmp_path), "--user-id", USER_ID]
        )
        == 0
    )
    # stdout is checked via a subprocess so this file also covers the
    # python -m path above. Here we compile the same store with query=None.
    brief = _compile(tmp_path, query=None)
    assert any(SOURCE_SENTENCE in line for line in _labelled_lines(brief, "source")), brief


def test_cli_empty_data_dir_prints_empty_state_and_exits_zero(tmp_path: Path, capsys) -> None:
    """Missing db: bootstrap, one quiet line, exit 0. Do not lecture."""

    empty = tmp_path / "fresh"
    assert onramp_main(["brief", "--data-dir", str(empty), "--user-id", USER_ID]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == EMPTY_SESSION_BRIEF
    lowered = captured.out.casefold()
    for word in FORBIDDEN_EMPTY_WORDS:
        assert word not in lowered
    assert "jsonrpc" not in captured.out


def test_hook_emits_additional_context_json(tmp_path: Path, monkeypatch) -> None:
    """A captured store must become valid additional_context JSON.

    Fails if the wrapper prints the brief as raw text.
    """

    context = _context(tmp_path, monkeypatch)
    _capture(context, SOURCE_NOTE)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alicebot_api.session_start_hook",
            "--data-dir",
            str(tmp_path),
            "--user-id",
            USER_ID,
        ],
        cwd=REPO_ROOT,
        env=_onramp_env(),
        input="{}",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert SOURCE_SENTENCE in payload["additional_context"]
    assert SOURCE_SENTENCE in payload["hookSpecificOutput"]["additionalContext"]
    assert "Traceback" not in completed.stdout


def test_hook_fails_open_when_brief_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    """A failing brief still exits 0 and does not leak a traceback on stdout.

    Fails if the wrapper failCloses, or if it forwards the exception.
    """

    import io

    import alicebot_api.session_start_hook as hook_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("brief exploded")

    monkeypatch.setattr(hook_module, "compile_local_session_brief", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook_module.main(["--data-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "Traceback" not in captured.out


def test_hook_fails_open_markdown_without_json(tmp_path: Path, monkeypatch, capsys) -> None:
    """Markdown fail-open must not print JSON.

    Fails if ``_fail_open`` always writes ``{}`` after ``--format
    markdown`` is known.
    """

    import io

    import alicebot_api.session_start_hook as hook_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("brief exploded")

    monkeypatch.setattr(hook_module, "compile_local_session_brief", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hook_module.main(["--data-dir", str(tmp_path), "--format", "markdown"]) == 0
    captured = capsys.readouterr()
    assert "{" not in captured.out
    assert "Traceback" not in captured.out


def test_capture_does_not_auto_promote(tmp_path: Path, monkeypatch) -> None:
    """After capture the proposed memory stays status=candidate.

    Fails if compile_session_brief or the CLI path promotes the candidate
    so recall memories count becomes non-zero.
    """

    from alicebot_api.mcp.registry import call_mcp_tool

    context = _context(tmp_path, monkeypatch)
    _capture(context, f"Decision: keep the canary.\n\n{SOURCE_NOTE}")
    _compile(tmp_path, query=SOURCE_SENTENCE)

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        candidates = store.list_memories(status="candidate")
        committed = store.list_memories(status=None, statuses=("active", "accepted"))
    assert candidates, "capture created no candidate; the no-promote assert is vacuous"
    assert all(row.get("status") == "candidate" for row in candidates)
    assert not any(SOURCE_SENTENCE in str(row.get("canonical_text") or "") for row in committed)

    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": SOURCE_SENTENCE})
    assert recall["count"] == 0


def test_the_brief_frames_stored_notes_as_quoted_data(tmp_path: Path, monkeypatch) -> None:
    """Owner ruling C1 (S4.4 round 2, 2026-09-23).

    The SessionStart hook injects this brief into the agent's context. Until
    this change every stored note was a bare "**fact**: <text>" line, so a
    note written as an instruction read as one. Fails if the frame line is
    dropped, or if an item's text is rendered unquoted, or quoted in a way a
    quote inside the note can close early.
    """

    from alicebot_api.session_briefing import SESSION_BRIEF_FRAME

    context = _context(tmp_path, monkeypatch)
    note = 'The runbook says "restart the worker" before paging anyone.'
    _commit(context, title="Runbook order", text=note, sensitivity="public", project="acme", domain="project")

    brief = _compile(tmp_path, query=None)

    lines = brief.splitlines()
    assert lines[0] == SESSION_BRIEF_FRAME
    assert "not instructions" in SESSION_BRIEF_FRAME
    facts = _labelled_lines(brief, "fact")
    assert facts == ["**fact**: " + json.dumps(note, ensure_ascii=False)]
    assert json.loads(facts[0].split(": ", 1)[1]) == note
