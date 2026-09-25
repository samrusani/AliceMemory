"""alice-memory doctor: vault census, user-scoped, no review UI.

Put next to the session-brief tests. Each test names the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import (
    _KNOWN_COMMANDS,
    _normalized_argv,
    bootstrap_database,
    main as onramp_main,
    resolve_db_path,
    sqlite_url_for_path,
)
from alicebot_api.session_briefing import (
    COMMITTED_MEMORY_STATUSES,
    EMPTY_SESSION_BRIEF,
    SESSION_BRIEF_TOKEN_BUDGET,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vault_doctor import COMMITTED_FACT_COUNT_SQL, COUNT_SQL_TEXTS
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import estimate_item_tokens

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"

SOURCE_NOTE = "# Vault canary\n\nThe indigo-lighthouse-42 canary stays in the vault.\n"
COMMITTED_FACT = "We will keep the public acme launch checklist on Thursday."

REPORT_LABELS = (
    "db",
    "sources",
    "searchable chunks",
    "committed facts",
    "last brief",
    "candidates waiting",
    "sleep proposals",
)
FORBIDDEN_PHRASES = ("review console", "/vnext", "clear the queue", "open Memory Review")


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


def _capture(context, raw_text: str) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    payload = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": raw_text,
            "title": "Vault canary",
            "domain": "personal",
            "sensitivity": "private",
        },
    )
    assert payload["status"] == "imported", payload
    return payload


def _commit(context) -> str:
    from alicebot_api.mcp.registry import call_mcp_tool

    payload = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": "Public acme launch",
            "canonical_text": COMMITTED_FACT,
            "memory_type": "decision",
            "domain": "project",
            "sensitivity": "public",
            "confidence": 0.96,
            "project_scope": ["acme"],
            "rationale": "User said: remember this",
        },
    )
    assert payload["status"] == "committed", payload
    return str(payload["memory"]["id"])


def _doctor_stdout(data_dir: Path, capsys) -> str:
    assert onramp_main(["doctor", "--data-dir", str(data_dir), "--user-id", USER_ID]) == 0
    return capsys.readouterr().out


def _line_value(report: str, label: str) -> str:
    prefix = f"{label}: "
    for line in report.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing {label!r} in report:\n{report}")


def _int_value(report: str, label: str) -> int:
    raw = _line_value(report, label)
    return int(raw.split()[0])


def _labels(report: str) -> list[str]:
    return [line.split(":", 1)[0] for line in report.splitlines() if line.strip()]


def test_empty_data_dir_prints_zeros_and_resolved_db_path(tmp_path: Path, capsys) -> None:
    """Empty --data-dir: honest zeros and the resolved db file.

    Fails if doctor skips bootstrap, invents a non-zero census, or prints
    a review-console sentence.
    """

    empty = tmp_path / "fresh"
    report = _doctor_stdout(empty, capsys)
    db_path = resolve_db_path(data_dir=str(empty), db=None)

    assert _line_value(report, "db") == str(db_path)
    assert db_path.is_file()
    assert _int_value(report, "sources") == 0
    assert _int_value(report, "searchable chunks") == 0
    assert _int_value(report, "committed facts") == 0
    assert _int_value(report, "candidates waiting") == 0
    assert _int_value(report, "sleep proposals") == 0
    assert _int_value(report, "last brief") == estimate_item_tokens({"text": EMPTY_SESSION_BRIEF})
    assert f"/ {SESSION_BRIEF_TOKEN_BUDGET} tokens" in _line_value(report, "last brief")
    lowered = report.casefold()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.casefold() not in lowered, report
    assert "jsonrpc" not in report


def test_capture_and_commit_list_chunks_and_facts_before_candidates(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """After capture and commit, sources/chunks/facts print before candidates.

    Mutation: print candidates before sources. This test fails.
    """

    context = _context(tmp_path, monkeypatch)
    capture = _capture(context, SOURCE_NOTE)
    _commit(context)
    report = _doctor_stdout(tmp_path, capsys)

    assert _int_value(report, "sources") >= 1
    assert _int_value(report, "searchable chunks") >= 1
    assert _int_value(report, "searchable chunks") >= int(capture.get("chunk_count") or 0)
    assert _int_value(report, "committed facts") >= 1
    assert _labels(report) == list(REPORT_LABELS)
    assert report.index("searchable chunks:") < report.index("candidates waiting:")
    assert report.index("committed facts:") < report.index("candidates waiting:")
    lowered = report.casefold()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.casefold() not in lowered, report


def test_doctor_missing_from_known_commands_becomes_mcp() -> None:
    """If doctor is missing from _KNOWN_COMMANDS, argv handling fails like brief.

    Mutation: drop ``doctor`` from ``_KNOWN_COMMANDS``. This test fails.
    """

    assert "doctor" in _KNOWN_COMMANDS
    assert _normalized_argv(["doctor", "--data-dir", "/tmp/x"]) == [
        "doctor",
        "--data-dir",
        "/tmp/x",
    ]


def test_report_does_not_send_anyone_to_review(tmp_path: Path, capsys) -> None:
    """The report must not name a review UI.

    Mutation: add ``review console``, ``/vnext``, or ``clear the queue``.
    This test fails.
    """

    report = _doctor_stdout(tmp_path / "quiet", capsys)
    lowered = report.casefold()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.casefold() not in lowered, report


def test_counts_bind_user_id_and_ignore_a_second_user(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A COUNT that drops user_id must pick up the other user's rows.

    Mutation: delete ``user_id`` from a doctor COUNT. This test fails.
    """

    assert COMMITTED_MEMORY_STATUSES == ("active", "accepted")
    assert "status IN (?, ?)" in COMMITTED_FACT_COUNT_SQL
    for sql in COUNT_SQL_TEXTS:
        assert "user_id" in sql
        assert "?" in sql
        assert "IN ({" not in sql

    context = _context(tmp_path, monkeypatch)
    _capture(context, SOURCE_NOTE)
    _commit(context)
    before = _doctor_stdout(tmp_path, capsys)
    before_counts = (
        _int_value(before, "sources"),
        _int_value(before, "searchable chunks"),
        _int_value(before, "committed facts"),
        _int_value(before, "candidates waiting"),
    )

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    with sqlite_user_connection(database, OTHER_USER_ID) as connection:
        ensure_sqlite_user(connection, OTHER_USER_ID, "other@alice", "Other")
        store = SQLiteVNextStore(connection, OTHER_USER_ID)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Other user source",
                "content_hash": "other-user-doctor-source-hash",
                "domain": "personal",
                "sensitivity": "private",
            }
        )
        store.create_source_chunk(
            {
                "source_id": source["id"],
                "chunk_index": 0,
                "text": "Other user chunk that must not appear in the first user's census.",
            }
        )
        store.create_memory(
            {
                "memory_key": "other-user-doctor-fact",
                "value": {"text": "Other user committed fact."},
                "memory_type": "decision",
                "title": "Other user fact",
                "canonical_text": "Other user committed fact.",
                "status": "active",
                "domain": "project",
                "sensitivity": "private",
            }
        )
        store.create_memory(
            {
                "memory_key": "other-user-doctor-candidate",
                "value": {"text": "Other user candidate."},
                "memory_type": "semantic",
                "title": "Other user candidate",
                "canonical_text": "Other user candidate.",
                "status": "candidate",
                "domain": "personal",
                "sensitivity": "private",
            }
        )

    after = _doctor_stdout(tmp_path, capsys)
    after_counts = (
        _int_value(after, "sources"),
        _int_value(after, "searchable chunks"),
        _int_value(after, "committed facts"),
        _int_value(after, "candidates waiting"),
    )
    assert after_counts == before_counts
    assert before_counts[0] >= 1
    assert before_counts[1] >= 1
    assert before_counts[2] >= 1


def test_token_line_uses_compile_local_session_brief(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Do not invent a second briefing algorithm.

    Mutation: skip ``compile_local_session_brief`` or estimate tokens
    against a different string. This test fails.
    """

    import alicebot_api.vault_doctor as doctor_module

    seen: dict[str, object] = {}

    def fake_brief(db_path, *, user_id, query):
        seen["db_path"] = db_path
        seen["user_id"] = str(user_id)
        seen["query"] = query
        return "labelled brief stand-in"

    monkeypatch.setattr(doctor_module, "compile_local_session_brief", fake_brief)
    _context(tmp_path, monkeypatch)
    report = _doctor_stdout(tmp_path, capsys)

    assert seen["query"] is None
    assert seen["user_id"] == USER_ID
    expected = estimate_item_tokens({"text": "labelled brief stand-in"})
    assert _int_value(report, "last brief") == expected
    assert f"/ {SESSION_BRIEF_TOKEN_BUDGET} tokens" in _line_value(report, "last brief")
