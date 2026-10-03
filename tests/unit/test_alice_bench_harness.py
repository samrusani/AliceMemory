"""The search-quality harness (scripts/alice_bench.py): build, recall, batch, search, fingerprint.

The harness measures what an agent reads when it searches an imported notes
folder. It calls private internals of the checkout (``MCPServer._handle_request``,
the importer's snapshot reader), so these tests are what makes a refactor of those
fail in CI. Everything here runs on the invented fixture corpus under
``tests/fixtures/search_quality`` and needs no network, no model and no paid call.

Every test names the mutation that must fail it. Test ids (TH1, TH2, ...) follow the
spec of the search-quality release.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest

import scripts.alice_bench as bench
from alicebot_api import onramp
from alicebot_api.mcp_tools import MCPRuntimeContext, call_mcp_tool
from alicebot_api.recall_framing import serialize_mcp_tool_result

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "search_quality"
CORPUS = FIXTURES / "corpus"
QUESTIONS = FIXTURES / "questions.json"
BENCH_SCRIPT = REPO_ROOT / "scripts" / "alice_bench.py"
USER_ID = UUID(bench.DEFAULT_USER_ID)


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One built vault of the fixture corpus, shared by the read-only tests of this file."""

    run = tmp_path_factory.mktemp("bench") / "run"
    code = bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--questions", str(QUESTIONS)])
    assert code == 0
    return run


def _db(run: Path) -> Path:
    return run / bench.VAULT_DIRNAME / bench.VAULT_FILENAME


def _cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the harness as a real command, with the checkout's source first on the path."""

    full_env = dict(os.environ if env is None else env)
    full_env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    return subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
        timeout=120,
    )


def _direct_recall_text(db: Path, query: str) -> str:
    """What the server's own serializer makes of the tool result: the reference for TH1."""

    context = MCPRuntimeContext(database_url=onramp.sqlite_url_for_path(db), user_id=USER_ID)
    return serialize_mcp_tool_result(call_mcp_tool(context, name="alice_recall", arguments={"query": query}))


def _stdio_recall_texts(db: Path, queries: list[str]) -> list[str]:
    """The text a real stdio server sends for each query, over the JSON-line transport."""

    env = bench.scrub_environment(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for index, query in enumerate(queries, start=2):
        lines.append(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "alice_recall", "arguments": {"query": query}},
            }
        )
    payload = b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)
    completed = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "mcp", "--db", str(db)],
        input=payload,
        capture_output=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")[-400:]
    by_id = {}
    for raw in completed.stdout.splitlines():
        if raw.strip():
            message = json.loads(raw)
            by_id[message["id"]] = message
    texts: list[str] = []
    for index in range(2, len(queries) + 2):
        content = by_id[index]["result"]["content"]
        assert len(content) == 1
        texts.append(content[0]["text"])
    return texts


def _questions() -> bench.QuestionSet:
    return bench.load_questions(QUESTIONS)


# TH1 ---------------------------------------------------------------------


def test_the_harness_recall_text_is_the_servers_serialized_result_and_a_stdio_servers(fixture_run: Path) -> None:
    """TH1. The text the harness scores is the text a host would show the model.

    Mutation: pretty-print the JSON in ``McpSession.recall`` (for example return
    ``json.dumps(json.loads(text), indent=1)``), or drop the ``framing`` key from it.
    Either one makes the harness text differ from ``serialize_mcp_tool_result`` and from
    what a real stdio server sends for the same query.
    """

    db = _db(fixture_run)
    qset = _questions()
    queries = [qset.questions[0].question, qset.questions[4].keyword_query or "", "nothing in the corpus matches this"]
    with bench.scoped_environment():
        session = bench.McpSession(db)
        harness = [session.recall(query) for query in queries]
        direct = [_direct_recall_text(db, query) for query in queries]
    stdio = _stdio_recall_texts(db, queries)
    assert harness == direct
    assert harness == stdio
    assert [hashlib.sha1(text.encode()).hexdigest() for text in harness] == [
        hashlib.sha1(text.encode()).hexdigest() for text in stdio
    ]
    assert harness[0].startswith('{"framing":')
    assert json.loads(harness[0])["sources"], "the fixture question must find a source"


def test_two_recalls_give_the_same_bytes_and_change_no_row(fixture_run: Path) -> None:
    """The harness reads and never writes: a batch is deterministic and leaves every table alone.

    Mutation: make a recall write a row (for example log an event), or make the output depend on
    the clock. ``batch`` compares row counts, ``event_log`` included, before and after and refuses
    to save outputs when they differ, and two batches must give identical bytes.
    """

    db = _db(fixture_run)
    first = fixture_run / "first.json"
    second = fixture_run / "second.json"
    before = bench.vault_row_counts(db)
    assert "event_log" in before
    for target in (first, second):
        code = bench.main(["batch", "--run-dir", str(fixture_run), "--questions", str(QUESTIONS), "--out", str(target)])
        assert code == 0
    assert bench.vault_row_counts(db) == before
    assert json.loads(first.read_text())["outputs"] == json.loads(second.read_text())["outputs"]


def test_a_recall_that_writes_a_row_stops_the_batch_and_saves_nothing(
    fixture_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The harness refuses to save outputs from a recall that changed the vault.

    Mutation: remove the ``before != after`` comparison in ``_cmd_batch``. A recall that writes
    (an event, a trace, a counter) would then pass for a read, and a vault would drift between runs.
    """

    db = _db(fixture_run)
    original = bench.McpSession.recall
    writes = {"count": 0}

    def writing_recall(self: bench.McpSession, query: str) -> str:
        import sqlite3

        writes["count"] += 1
        connection = sqlite3.connect(db)
        connection.execute("INSERT OR REPLACE INTO alice_schema_state (key, value) VALUES (?, ?)", (f"probe{writes['count']}", "x"))
        connection.commit()
        connection.close()
        return original(self, query)

    monkeypatch.setattr(bench.McpSession, "recall", writing_recall)
    target = fixture_run / "refused.json"
    try:
        code = bench.main(["batch", "--run-dir", str(fixture_run), "--questions", str(QUESTIONS), "--out", str(target)])
        assert code == bench.EXIT_REFUSED
        assert "changed rows" in capsys.readouterr().err
        assert not target.exists()
    finally:
        import sqlite3

        connection = sqlite3.connect(db)
        connection.execute("DELETE FROM alice_schema_state WHERE key LIKE 'probe%'")
        connection.commit()
        connection.close()


# TH2 ---------------------------------------------------------------------


def _runner_ok() -> bench.SearchResult:
    return bench.SearchResult(text="ok", status="ok", truncated=False, raw_bytes=2)


def _runner_fails() -> bench.SearchResult:
    raise bench.BenchError("the search itself failed")


def _log_lines(state_dir: Path) -> list[dict[str, object]]:
    path = state_dir / "search_log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_fourth_search_is_refused_and_exactly_three_log_lines_stay(tmp_path: Path) -> None:
    """TH2. Three searches, then a refusal that writes no log line.

    Mutation: remove the budget check, or write a log line for a refused search.
    """

    for number in range(3):
        result = bench.run_budgeted_search(tmp_path, arm="alice", search_input=f"q{number}", budget=3, runner=_runner_ok)
        assert result.status == "ok"
    with pytest.raises(bench.BudgetExhausted):
        bench.run_budgeted_search(tmp_path, arm="alice", search_input="q4", budget=3, runner=_runner_ok)
    lines = _log_lines(tmp_path)
    assert [line["n"] for line in lines] == [1, 2, 3]
    assert {"input", "bytes", "truncated", "sha1", "arm", "status"} <= set(lines[0])


def test_a_search_that_fails_still_spends_one_of_the_three(tmp_path: Path) -> None:
    """TH2. The attempt is counted before it runs, so an agent cannot probe with bad searches for free.

    Mutation: count only successful searches (increment the counter after the runner returns).
    """

    for number in range(3):
        result = bench.run_budgeted_search(tmp_path, arm="grep", search_input=f"bad{number}", budget=3, runner=_runner_fails)
        assert result.status == "error"
    with pytest.raises(bench.BudgetExhausted):
        bench.run_budgeted_search(tmp_path, arm="grep", search_input="fourth", budget=3, runner=_runner_ok)
    assert [line["status"] for line in _log_lines(tmp_path)] == ["error", "error", "error"]


def test_a_second_search_waits_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    """TH2. The counter and the log are locked together, so two callers of one run cannot interleave.

    Mutation: remove the file lock (the ``with _locked(...)`` block of ``run_budgeted_search``).
    Without it the second search runs while the first is still inside its runner.
    """

    first_inside = threading.Event()
    second_ran = threading.Event()
    observed: dict[str, bool] = {}

    def slow_runner() -> bench.SearchResult:
        first_inside.set()
        time.sleep(0.6)
        observed["second_ran_while_first_was_inside"] = second_ran.is_set()
        return _runner_ok()

    def second_runner() -> bench.SearchResult:
        second_ran.set()
        return _runner_ok()

    def first() -> None:
        bench.run_budgeted_search(tmp_path, arm="alice", search_input="first", budget=3, runner=slow_runner)

    def second() -> None:
        assert first_inside.wait(timeout=10)
        bench.run_budgeted_search(tmp_path, arm="alice", search_input="second", budget=3, runner=second_runner)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert observed == {"second_ran_while_first_was_inside": False}
    assert [line["input"] for line in _log_lines(tmp_path)] == ["first", "second"]


def test_six_processes_of_one_run_share_one_counter(fixture_run: Path) -> None:
    """TH2. Real commands in parallel: exactly three get an answer and three are refused.

    Mutation: give each process its own counter (for example keep the count in memory), or drop
    the lock. More than three would then run, or the log would hold the wrong number of lines.
    """

    state = fixture_run / bench.STATE_DIRNAME / "shared"
    args = [
        sys.executable,
        str(BENCH_SCRIPT),
        "search",
        "--arm",
        "grep",
        "--run-dir",
        str(fixture_run),
        "--state-dir",
        str(state),
        "--pattern",
        "Marta",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    processes = [
        subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True) for _ in range(6)
    ]
    codes = []
    for process in processes:
        process.communicate(timeout=120)
        codes.append(process.returncode)
    assert sorted(codes) == [bench.EXIT_OK] * 3 + [bench.EXIT_BUDGET] * 3
    assert len(_log_lines(state)) == 3
    assert int((state / "search.count").read_text()) == 3


# TH3 ---------------------------------------------------------------------


def test_the_byte_cap_binds_the_grep_arm_and_the_cut_is_recorded(tmp_path: Path) -> None:
    """TH3. Grep output is cut at the cap, the cut is stated, and the log says so.

    Mutation: drop the cap (pass ``cap=None`` for the capped arm), or stop recording ``truncated``.
    """

    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "long.md").write_text("\n".join(f"line {number} of the long note" for number in range(400)), encoding="utf-8")
    capped = bench.grep_search(snapshot, pattern="long note", options="", cap=1000)
    assert capped.truncated is True
    assert capped.raw_bytes > 1000
    assert capped.text.endswith("[output cut at 1000 bytes]\n")
    assert len(capped.text.encode()) < 1100
    uncapped = bench.grep_search(snapshot, pattern="long note", options="", cap=None)
    assert uncapped.truncated is False
    assert uncapped.text.count("long.md:") == 400

    gates = bench.load_gates()
    assert gates.grep_cap == 16384
    state = tmp_path / "state"
    result = bench.run_budgeted_search(
        state,
        arm="grep",
        search_input="long note",
        budget=3,
        runner=lambda: bench.grep_search(snapshot, pattern="long note", options="", cap=1000),
    )
    assert result.truncated
    assert _log_lines(state)[0]["truncated"] is True


def test_the_alice_arm_keeps_limit_depth_and_sources_at_their_defaults() -> None:
    """TH3. The wrapper sends only the query and refuses any other setting.

    Mutation: accept ``limit`` 50 (or any value other than the default) in ``alice_arm_arguments``.
    """

    assert bench.alice_arm_arguments("a question") == {"query": "a question"}
    assert bench.alice_arm_arguments("q", limit=8, context_depth="low", include_sources=True) == {"query": "q"}
    for kwargs in (
        {"limit": 50},
        {"limit": 1},
        {"context_depth": "high"},
        {"include_sources": False},
        {"debug": True},
        {"projects": ["x"]},
    ):
        with pytest.raises(bench.PinnedSettingError):
            bench.alice_arm_arguments("q", **kwargs)  # type: ignore[arg-type]
    from alicebot_api.mcp.shared import _RECALL_DEFAULT_LIMIT

    assert bench.PINNED_RECALL_DEFAULTS["limit"] == _RECALL_DEFAULT_LIMIT


def test_the_search_command_refuses_a_pinned_setting_and_a_grep_flag_that_reads_files(fixture_run: Path) -> None:
    """TH3. At the command line too, and a refusal spends no search.

    Mutation: accept ``--limit 50``, or allow ``-f`` or ``--include`` in ``validate_grep_options``.
    """

    state = fixture_run / bench.STATE_DIRNAME / "refusals"
    common = ["search", "--run-dir", str(fixture_run), "--state-dir", str(state)]
    refused = _cli(*common, "--arm", "alice", "--query", "spare key", "--limit", "50")
    assert refused.returncode == bench.EXIT_REFUSED
    assert "pinned" in refused.stderr
    for options in ("-f", "-rf", "-f /etc/hosts", "--include=*.md", "-e other", "-i;ls", "-C"):
        result = _cli(*common, "--arm", "grep", "--pattern", "key", f"--grep-options={options}")
        assert result.returncode == bench.EXIT_REFUSED, options
    assert not (state / "search.count").exists()
    assert bench.validate_grep_options("-i -n -C 2") == ["-i", "-n", "-C", "2"]
    assert bench.validate_grep_options("-rn -A2") == ["-rn", "-A2"]


def test_the_search_command_runs_both_arms_and_logs_them(fixture_run: Path) -> None:
    """The wrapper an answering agent runs works for the Alice arm and the grep arms.

    Mutation: make the Alice arm call a different tool, or have the grep arm search the raw corpus
    instead of the snapshot.
    """

    state = fixture_run / bench.STATE_DIRNAME / "arms"
    common = ["search", "--run-dir", str(fixture_run), "--state-dir", str(state)]
    alice = _cli(*common, "--arm", "alice", "--query", "Who has the spare key to the brass cabinet?")
    assert alice.returncode == 0, alice.stderr
    excerpts = [bench.unquote_leaf(entry["excerpt"]) for entry in json.loads(alice.stdout)["sources"]]
    assert any("Marta Quill holds the only spare key" in excerpt for excerpt in excerpts)
    grep = _cli(*common, "--arm", "grep", "--pattern", "spare key", "--grep-options=-n")
    assert grep.returncode == 0, grep.stderr
    assert "tide-keeping.md" in grep.stdout and "spare key" in grep.stdout
    no_match = _cli(*common, "--arm", "grep-uncapped", "--pattern", "zzzz-not-here")
    assert no_match.returncode == 0
    assert no_match.stdout.strip() == ""
    assert [line["arm"] for line in _log_lines(state)] == ["alice", "grep", "grep-uncapped"]
    assert _cli(*common, "--arm", "grep", "--pattern", "x").returncode == bench.EXIT_BUDGET


# TH7 ---------------------------------------------------------------------


def test_scrub_environment_removes_every_alice_variable_except_the_ones_the_arm_sets() -> None:
    """TH7. A key or an endpoint in the parent never reaches a run.

    Mutation: stop scrubbing one prefix (for example only remove names that start with ``ALICE_``,
    which keeps ``ALICEBOT_AUTH_USER_ID``), or drop the arm's own variable.
    """

    parent = {
        "PATH": "/usr/bin",
        "ALICE_AGENT_API_KEY": "k",
        "ALICE_EMBEDDINGS_BASE_URL": "http://127.0.0.1:1",
        "ALICE_MCP_FULL_TOOLS": "1",
        "ALICEBOT_AUTH_USER_ID": "u",
        "ALICE_SEARCH_QUALITY": "on",
        "HOME": "/home/x",
    }
    assert bench.scrub_environment(parent) == {"PATH": "/usr/bin", "HOME": "/home/x"}
    assert bench.scrub_environment(parent, allow={"ALICE_SEARCH_QUALITY": "passage"}) == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "ALICE_SEARCH_QUALITY": "passage",
    }
    with bench.scoped_environment({"ALICE_SEARCH_QUALITY": "off"}) as removed:
        assert not [name for name in os.environ if name.startswith("ALICE") and name != "ALICE_SEARCH_QUALITY"]
        assert os.environ["ALICE_SEARCH_QUALITY"] == "off"
        assert isinstance(removed, list)
    assert os.environ.get("ALICE_SEARCH_QUALITY") != "off"


class _RecordingStub:
    """A loopback server that records every request and answers 500."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server hook name
                outer.requests.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> _RecordingStub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_an_embeddings_endpoint_and_an_agent_key_in_the_parent_never_reach_a_run(fixture_run: Path) -> None:
    """TH7. The stub gets zero requests, and a bogus key changes nothing.

    Mutation: stop scrubbing ``ALICE_EMBEDDINGS_BASE_URL`` or ``ALICE_AGENT_API_KEY``. The recall
    would then send the query to the stub, or fail on the invalid key.
    """

    db = _db(fixture_run)
    query = "Who has the spare key to the brass cabinet?"
    with bench.scoped_environment():
        expected = bench.McpSession(db).recall(query)

    with _RecordingStub() as stub:
        polluted = dict(os.environ)
        polluted.update(
            {
                "ALICE_EMBEDDINGS_BASE_URL": stub.url,
                "ALICE_EMBEDDINGS_MODEL": "stub-model",
                "ALICE_AGENT_API_KEY": "not-a-real-key",
                "ALICEBOT_AUTH_USER_ID": "11111111-1111-1111-1111-111111111111",
            }
        )
        harness = _cli("recall", "--run-dir", str(fixture_run), "--query", query, env=polluted)
        assert harness.returncode == 0, harness.stderr
        assert harness.stdout.rstrip("\n") == expected
        assert stub.requests == []

        # The controls: the same environment with no scrub reaches the key and the stub.
        with mock.patch.dict(os.environ, polluted):
            unscrubbed = bench.McpSession(db).call("alice_recall", {"query": query})
        assert unscrubbed[0] is True, "an invalid key must break an unscrubbed recall"
        keyless = {name: value for name, value in polluted.items() if name != "ALICE_AGENT_API_KEY"}
        with mock.patch.dict(os.environ, keyless):
            bench.McpSession(db).call("alice_recall", {"query": query})
        assert stub.requests, "the stub would have seen the query if the scrub failed"


# TH8 ---------------------------------------------------------------------


def test_a_data_dir_outside_the_run_directory_is_refused(fixture_run: Path, tmp_path: Path) -> None:
    """TH8. The harness can never open a real vault.

    Mutation: drop the check in ``resolve_inside``. A vault outside the run directory would then be
    opened, and so would a symlink that leads out of it.
    """

    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    shutil.copy(_db(fixture_run), outside / bench.VAULT_FILENAME)
    result = _cli("recall", "--run-dir", str(fixture_run), "--data-dir", str(outside), "--query", "spare key")
    assert result.returncode == bench.EXIT_REFUSED
    assert "outside the run directory" in result.stderr
    assert result.stdout == ""
    link = fixture_run / "sneaky"
    link.symlink_to(outside, target_is_directory=True)
    sneaky = _cli("recall", "--run-dir", str(fixture_run), "--data-dir", str(link), "--query", "spare key")
    assert sneaky.returncode == bench.EXIT_REFUSED
    inside = _cli("recall", "--run-dir", str(fixture_run), "--data-dir", str(fixture_run / bench.VAULT_DIRNAME), "--query", "spare key")
    assert inside.returncode == 0
    state = _cli("search", "--arm", "grep", "--run-dir", str(fixture_run), "--state-dir", str(outside), "--pattern", "key")
    assert state.returncode == bench.EXIT_REFUSED
    assert not (outside / "search.count").exists()


def test_a_directory_that_is_not_a_harness_run_directory_is_refused(fixture_run: Path, tmp_path: Path) -> None:
    """TH8. ``--run-dir`` cannot be pointed at a directory that already holds someone's files.

    The occupied directory below holds a working vault, so only the marker check can stop a read
    of it, and a build into it must leave the owner's file as it was.

    Mutation: drop the marker check in ``prepare_run_dir`` (build) or in ``require_run_dir`` (every
    other command).
    """

    occupied = tmp_path / "occupied"
    (occupied / bench.VAULT_DIRNAME).mkdir(parents=True)
    shutil.copy(_db(fixture_run), occupied / bench.VAULT_DIRNAME / bench.VAULT_FILENAME)
    (occupied / "notes.txt").write_text("not mine", encoding="utf-8")
    result = _cli("build", "--run-dir", str(occupied), "--corpus", str(CORPUS))
    assert result.returncode == bench.EXIT_REFUSED
    assert "not empty" in result.stderr
    assert (occupied / "notes.txt").read_text() == "not mine"
    recall = _cli("recall", "--run-dir", str(occupied), "--query", "spare key")
    assert recall.returncode == bench.EXIT_REFUSED
    assert "not made by this harness" in recall.stderr
    assert recall.stdout == ""


# TH9 ---------------------------------------------------------------------


def test_the_fingerprint_names_what_a_number_measured_and_not_the_version_string(
    fixture_run: Path, tmp_path: Path
) -> None:
    """TH9. Sha, dirty flag, the source hash, the import path, the tools digest, the switch, the order and every hash.

    Mutation: record ``alicebot_api.__version__`` (it reads installed metadata and can name another
    build), leave one hash out of the fingerprint (the source hash included), or leave out the Python and
    SQLite versions the full-text ranking ran on.
    """

    gates_data = json.loads((REPO_ROOT / "gates.json").read_text())
    gates_data["hashes"]["prompts"] = {
        "alice_arm": "a" * 64,
        "grep_arm": "b" * 64,
        "no_search": "c" * 64,
        "judge": "d" * 64,
    }
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps(gates_data), encoding="utf-8")
    gates = bench.load_gates(gates_path)
    manifest = json.loads((fixture_run / bench.MANIFEST_FILENAME).read_text())
    with bench.scoped_environment({"ALICE_SEARCH_QUALITY": "passage"}):
        session = bench.McpSession(_db(fixture_run))
        tools = session.tools_list()
        print_ = bench.fingerprint(
            repo=REPO_ROOT,
            gates=gates,
            session=session,
            manifest=manifest,
            search_quality="passage",
            question_set_sha256=_questions().sha256,
            answerer_model="answerer-x",
            judge_model="judge-y",
        )
    git = bench.git_state(REPO_ROOT)
    assert print_["git_sha"] == git["git_sha"] and len(str(print_["git_sha"])) == 40
    assert print_["git"] == "present" and isinstance(print_["dirty"], bool)
    assert print_["checkout_source_sha256"] == bench.checkout_source_sha256(REPO_ROOT)
    assert print_["vault_build"]["checkout_source_sha256"] == print_["checkout_source_sha256"]
    real_file = Path(str(print_["alicebot_api_file"]))
    assert real_file == real_file.resolve()
    assert real_file.is_relative_to(REPO_ROOT / "apps" / "api" / "src")
    assert print_["alicebot_api_inside_checkout"] is True
    assert print_["tools_list_digest"] == hashlib.sha256(
        json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert [tool["name"] for tool in tools] == ["alice_memory_commit", "alice_recall", "alice_resume"]
    assert print_["search_quality"] == "passage"
    assert print_["import_order"] == "sorted"
    assert print_.get("prompt_hashes") == gates_data["hashes"]["prompts"]
    assert print_["gates_sha256"] == hashlib.sha256(gates_path.read_bytes()).hexdigest()
    assert print_["harness_sha256"] == hashlib.sha256(BENCH_SCRIPT.read_bytes()).hexdigest()
    assert print_["corpus_hash"] == manifest["corpus_hash"] and print_["snapshot_hash"] == manifest["snapshot_hash"]
    assert print_["question_set_sha256"] == _questions().sha256
    assert (print_["answerer_model"], print_["judge_model"]) == ("answerer-x", "judge-y")
    assert print_["recall_limit"] == 8 and print_["byte_budgets"] == [4096, 8192]
    import alicebot_api

    encoded = json.dumps(print_)
    assert not {"version", "package_version", "alicebot_api_version", "__version__"} & set(print_)
    assert alicebot_api.__version__ not in encoded
    assert (print_["python_version"], print_["sqlite_version"]) == (platform.python_version(), sqlite3.sqlite_version)


# TH10 --------------------------------------------------------------------


def test_the_grep_snapshot_is_built_through_the_importers_credential_filter(tmp_path: Path) -> None:
    """TH10. A line the importer withholds is in neither arm's text, and the build logs it.

    Mutation: write the raw corpus files into the snapshot (copy them) instead of the stored text.
    The planted line would then be in grep's files and a grep for it would find it.
    """

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    planted = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    (corpus / "ops-notes.md").write_text(
        "# Ops notes\n\n## Keys\n\nThe deploy token is " + planted + " and it rotates monthly.\n\n"
        "## Contacts\n\nThe duty phone is on the fridge.\n",
        encoding="utf-8",
    )
    (corpus / "plain.md").write_text("# Plain\n\n## Only\n\nNothing secret here.\n", encoding="utf-8")
    run = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run), "--corpus", str(corpus)]) == 0
    manifest = json.loads((run / bench.MANIFEST_FILENAME).read_text())
    assert [entry["file"] for entry in manifest["withheld"]] == ["ops-notes.md"]
    assert all("ghp_" not in item for entry in manifest["withheld"] for item in entry["items"])
    assert any("line" in item for item in manifest["withheld"][0]["items"])
    assert planted in (corpus / "ops-notes.md").read_text(), "the control: the raw file does hold the token"
    for path in (run / bench.SNAPSHOT_DIRNAME).rglob("*"):
        if path.is_file():
            assert planted not in path.read_text()
    found = bench.grep_search(run / bench.SNAPSHOT_DIRNAME, pattern="ghp_", options="", cap=None)
    assert found.text == ""
    kept = bench.grep_search(run / bench.SNAPSHOT_DIRNAME, pattern="duty phone", options="", cap=None)
    assert "ops-notes.md" in kept.text
    with bench.scoped_environment():
        text = bench.McpSession(_db(run)).recall("deploy token rotates monthly")
    assert planted not in text


# TH14 --------------------------------------------------------------------


def test_build_order_gives_three_different_capture_orders(tmp_path: Path) -> None:
    """TH14. sorted, reverse and a seeded shuffle really change the order the vault is captured in.

    Mutation: ignore the order flag in ``order_files`` (always return the sorted list). The three
    manifests would then name one capture order, and so would the vaults' capture times.
    """

    orders: dict[str, list[str]] = {}
    for order in ("sorted", "reverse", "shuffle:7"):
        run = tmp_path / order.replace(":", "-")
        assert bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--order", order]) == 0
        manifest = json.loads((run / bench.MANIFEST_FILENAME).read_text())
        assert manifest["order"] == order
        orders[order] = manifest["capture_order"]
        by_time = [source["external_id"] for source in bench.read_vault_sources(_db(run))]
        assert by_time == manifest["capture_order"], "the manifest names the order the vault was captured in"
    assert orders["sorted"] == sorted(orders["sorted"])
    assert orders["reverse"] == list(reversed(orders["sorted"]))
    assert orders["shuffle:7"] not in (orders["sorted"], orders["reverse"])
    assert sorted(orders["shuffle:7"]) == orders["sorted"]
    again = bench.order_files(bench.read_corpus(CORPUS)[1], "shuffle:7")
    assert [item.relative_path for item in again] == orders["shuffle:7"], "a seed gives the same order twice"
    with pytest.raises(bench.BenchError):
        bench.order_files(bench.read_corpus(CORPUS)[1], "sideways")


def test_a_second_build_into_the_same_run_directory_needs_rebuild(tmp_path: Path) -> None:
    """A vault is rebuilt for every checkout and corpus, never cached or topped up.

    Mutation: let ``build`` write into an existing build without ``--rebuild``. A second import would
    then call every file a duplicate and the vault would be a mix of two builds.
    """

    run = tmp_path / "run"
    args = ["build", "--run-dir", str(run), "--corpus", str(CORPUS)]
    assert bench.main(args) == 0
    assert bench.main(args) == bench.EXIT_REFUSED
    assert bench.main([*args, "--rebuild", "--order", "reverse"]) == 0
    manifest = json.loads((run / bench.MANIFEST_FILENAME).read_text())
    assert manifest["sources"] == 8 and manifest["duplicates"] == 0
