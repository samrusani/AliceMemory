"""The showcase bench: questions, arms, a blind packet, and team keys.

These tests use invented notes. They call no paid model and no network, apart
from a loopback embeddings server started in one test.

Mutation: set SUPPORT_PROFILE to admin_agent. The support key would then read
the HR pay record, and test_a_support_key_does_not_read_hr_or_a_draft_price fails.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
import scripts.showcase_visibility as visibility
from alicebot_api.vnext_embeddings import OpenAICompatibleEmbeddingProvider

from tests.fixtures.showcase_company.company import ABSENT, Note


def test_each_size_has_fifteen_questions_per_task_and_the_questions_do_not_hold_the_answer() -> None:
    total = 0
    for size in (1000, 10000):
        document = showcase.questions_for(size)
        assert len(document["questions"]) == 7 * 15
        total += len(document["questions"])
        by_task: dict[str, int] = {}
        for question in document["questions"]:
            by_task[question["task"]] = by_task.get(question["task"], 0) + 1
            for needle in question["must_include"] + question["leak_strings"] + question["must_not_include"]:
                assert needle.casefold() not in question["question"].casefold()
        assert by_task == {task: 15 for task in showcase.TASKS}
    assert total == 210
    for absent in ABSENT:
        assert all(absent not in question["question"] for question in showcase.questions_for(1000)["questions"])


def test_the_rubric_scores_without_looking_at_the_arm() -> None:
    question = showcase.questions_for(1000)["questions"][0]
    assert question["task"] == "recall"
    assert showcase.score_answer(question, "The contract is QF-C-2041.")["correct"] is True
    assert showcase.score_answer(question, "I cannot tell.")["correct"] is False
    hidden = next(item for item in showcase.questions_for(1000)["questions"] if item["task"] == "permission")
    assert showcase.score_answer(hidden, "not known")["leak"] is False
    assert showcase.score_answer(hidden, "compensation is 128400 ledger units")["leak"] is True


def test_the_grading_packet_does_not_name_the_arm() -> None:
    questions = showcase.questions_for(1000)["questions"][:2]
    answers = {
        "alice": {question["id"]: "one" for question in questions},
        "grep": {question["id"]: "two" for question in questions},
    }
    packet, key = showcase.grade_packet(questions, answers, seed=7)
    blob = json.dumps(packet)
    assert "alice" not in blob and "grep" not in blob
    assert packet["items"][0]["answers"][0]["label"] == "A"
    again, _key = showcase.grade_packet(questions, answers, seed=7)
    assert packet == again
    assert set(key["items"][questions[0]["id"]].values()) == {"alice", "grep"}


def test_a_batch_resumes_and_keeps_the_first_answer(tmp_path: Path) -> None:
    questions = showcase.questions_for(1000)["questions"][:4]
    path = tmp_path / "answers.jsonl"
    first = {"id": questions[0]["id"], "arm": "alice", "team": "sales", "answer": "first"}
    assert showcase.record_answer(path, first) is True
    assert showcase.record_answer(path, {**first, "answer": "second"}) is False
    assert showcase.load_done(path)[(questions[0]["id"], "alice", "sales")]["answer"] == "first"
    batch = showcase.pending(questions, showcase.load_done(path), 2, arm="alice", team="sales")
    assert [item["id"] for item in batch] == [questions[1]["id"], questions[2]["id"]]


def test_a_second_arm_keeps_its_own_answer(tmp_path: Path) -> None:
    questions = showcase.questions_for(1000)["questions"][:1]
    path = tmp_path / "answers.jsonl"
    question_id = questions[0]["id"]
    assert showcase.record_answer(path, {"id": question_id, "arm": "alice", "team": "sales", "answer": "from alice"}) is True
    assert showcase.record_answer(path, {"id": question_id, "arm": "grep", "team": "sales", "answer": "from grep"}) is True
    done = showcase.load_done(path)
    assert done[(question_id, "alice", "sales")]["answer"] == "from alice"
    assert done[(question_id, "grep", "sales")]["answer"] == "from grep"
    assert showcase.pending(questions, done, 5, arm="grep", team="sales") == []
    assert [item["id"] for item in showcase.pending(questions, done, 5, arm="notes", team="sales")] == [question_id]


def test_overlapping_samples_are_named_and_a_wide_gap_is_not() -> None:
    rows = []
    for arm, correct in (("alice", 15), ("grep", 0)):
        rows.extend({"size": 1000, "arm": arm, "task": "recall", "correct": index < correct, "leak": False} for index in range(15))
    text = showcase.render_report(rows)
    assert "cannot separate" not in text
    close = []
    for arm, correct in (("alice", 8), ("grep", 7)):
        close.extend(
            {"size": 1000, "arm": arm, "task": "recall", "correct": index < correct, "leak": index == 0} for index in range(15)
        )
    close_text = showcase.render_report(close)
    assert "alice and grep overlap, so this sample cannot separate them" in close_text
    assert "leaks 1/15" in close_text


def test_the_model_settings_are_local_and_alice_can_read_ollama_embeddings() -> None:
    settings = showcase.embedding_settings("http://127.0.0.1:11434", "nomic-embed-text:v1.5")
    assert settings["ALICE_EMBEDDINGS_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert settings["ALICE_EMBEDDINGS_MODEL"] == "nomic-embed-text:v1.5"
    try:
        showcase.require_local_endpoint("https://ollama.com")
    except bench.BenchError:
        pass
    else:
        raise AssertionError("a cloud endpoint was accepted")
    config = showcase.mem0_config(
        chat_model="qwen2.5:7b",
        embed_model="nomic-embed-text:v1.5",
        base="http://127.0.0.1:11434",
        store_path=Path("mem0"),
    )
    assert config["llm"]["config"]["model"] == "qwen2.5:7b"
    assert config["embedder"]["provider"] == "ollama"

    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["path"] = self.path
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            seen["model"] = body["model"]
            payload = json.dumps({"data": [{"index": 0, "embedding": [0.25, 0.5]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://127.0.0.1:{port}/v1",
            model="nomic-embed-text:v1.5",
        )
        vector = provider.embed_text("quarry note")
    finally:
        server.shutdown()
    assert seen["path"] == "/v1/embeddings"
    assert seen["model"] == "nomic-embed-text:v1.5"
    assert vector[0] == 0.25


def test_the_none_arm_command_refuses(tmp_path: Path) -> None:
    run = tmp_path / "run"
    bench.prepare_run_dir(run, create=True)
    code = bench.main(["showcase-search", "--run-dir", str(run), "--arm", "none"])
    assert code != 0


def test_note_tools_stay_inside_the_folder_and_the_none_arm_has_no_tool(tmp_path: Path) -> None:
    run = tmp_path / "run"
    bench.prepare_run_dir(run, create=True)
    snapshot = run / "snapshot"
    snapshot.mkdir()
    (snapshot / "sales").mkdir()
    (snapshot / "sales" / "note.md").write_text("hello quarry\n", encoding="utf-8")
    (run / "showcase.json").write_text("{}\n", encoding="utf-8")
    assert "sales/note.md" in showcase.list_notes(snapshot, cap=1000)
    assert "quarry" in showcase.read_note(snapshot, "sales/note.md", cap=1000)
    try:
        showcase.read_note(snapshot, "../note.md", cap=1000)
    except bench.BenchError:
        pass
    else:
        raise AssertionError("a path outside the folder was read")
    try:
        showcase.none_search()
    except showcase.NoSearchTool:
        pass
    else:
        raise AssertionError("the none arm returned a tool")


def test_mem0_scopes_hide_a_note_the_team_may_not_read() -> None:
    """Scopes come from the questions-file policy. Alice's permission code is not asked."""

    assert showcase.SUPPORT_PROFILE == "memory_proposal_agent"
    hr = Note("hr.md", "hr", "2025-04-02", "confidential", "legal", "hr_note", "Pay", "compensation is 128400 ledger units", ())
    draft = Note(
        "draft.md",
        "sales",
        "2026-02-02",
        "private",
        "financial",
        "pricing_draft",
        "Draft",
        "Draft list price 910 ledger",
        ("leadership", "sales"),
    )
    ordinary = Note("note.md", "sales", "2025-01-01", "internal", "professional", "account_note", "Price", "published 520", ())
    scopes, shared = showcase.note_scopes((hr, draft, ordinary))
    assert "128400" not in "\n".join(scopes["support"])
    assert "128400" not in "\n".join(scopes["sales"])
    assert "128400" in "\n".join(scopes["leadership"])
    assert "910" in "\n".join(scopes["sales"])
    assert "910" not in "\n".join(scopes["support"])
    assert "520" in "\n".join(scopes["support"])
    assert "128400" in "\n".join(shared)
    memory = showcase.FakeMemory()
    for team, texts in scopes.items():
        for text in texts:
            memory.add(text, scope=team)
    assert memory.search("compensation", scope="support") == []
    assert "910" in "\n".join(memory.search("draft price", scope="sales"))


def _note(folder: Path, relative: str, team: str, sensitivity: str, domain: str, body: str) -> None:
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"team: {team}\n"
        "date: 2025-06-01\n"
        f"sensitivity: {sensitivity}\n"
        f"domain: {domain}\n"
        "kind: note\n"
        "---\n"
        "\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_a_support_key_does_not_read_hr_or_a_draft_price(tmp_path: Path) -> None:
    """Three notes, three keys, through the real importer and alice_recall.

    Mutation: set SUPPORT_PROFILE to admin_agent.
    """

    corpus = tmp_path / "notes"
    _note(corpus, "pricing/changes/current.md", "sales", "internal", "professional", "The published price is 520 ledger units.")
    _note(corpus, "pricing/drafts/secret.md", "sales", "private", "financial", "Draft list price 910 ledger for the kit.")
    _note(corpus, "hr/notes/pay.md", "hr", "confidential", "legal", "Mara Quen compensation is 128400 ledger units.")
    run = tmp_path / "run"
    db_path = showcase.import_corpus(run, corpus)
    keys = showcase._create_keys(db_path)
    (run / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
    support_pay = showcase.alice_search(run, team="support", query="compensation")
    support_draft = showcase.alice_search(run, team="support", query="draft")
    support_price = showcase.alice_search(run, team="support", query="published")
    sales_draft = showcase.alice_search(run, team="sales", query="draft")
    sales_pay = showcase.alice_search(run, team="sales", query="compensation")
    leadership = showcase.alice_search(run, team="leadership", query="compensation")
    assert "128400" not in support_pay
    assert "910" not in support_draft
    assert "520" in support_price
    assert "910" in sales_draft
    assert "128400" not in sales_pay
    assert "128400" in leadership


def test_a_note_removed_between_builds_is_not_recalled(tmp_path: Path) -> None:
    corpus = tmp_path / "notes"
    _note(corpus, "a.md", "sales", "internal", "professional", "amber quarry token stays")
    _note(corpus, "b.md", "sales", "internal", "professional", "cobalt ledger token goes")
    run = tmp_path / "run"
    showcase.import_corpus(run, corpus)
    (corpus / "b.md").unlink()
    showcase.import_corpus(run, corpus)
    keys = showcase._create_keys(run / "vault" / "memory.db")
    (run / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
    text = showcase.alice_search(run, team="leadership", query="cobalt")
    assert "cobalt ledger token" not in text
    kept = showcase.alice_search(run, team="leadership", query="amber")
    assert "amber quarry token" in kept


def test_the_visibility_module_does_not_import_alice_and_a_withheld_fact_is_a_leak() -> None:
    """The score reads may_know from the fact. A professional internal fact can still be withheld.

    Mutation: make may_know ask Alice. Support would then be allowed this fact, and the leak assertion fails.
    """

    fact = {
        "id": "withheld-ordinary",
        "kind": "account_note",
        "text": "quarry token",
        "may_know": ["leadership"],
        "domain": "professional",
        "sensitivity": "internal",
    }
    assert visibility.is_leak("saw the quarry token", "support", [fact]) is True
    assert visibility.is_leak("nothing useful", "support", [fact]) is False
    assert "support" not in visibility.may_know(visibility.POLICY[0])
    source = Path(visibility.__file__).read_text(encoding="utf-8")
    assert "alicebot_api" not in source
    document = showcase.questions_for(1000)
    assert document["role"] == "development"
    assert document["policy"] == visibility.policy_for_questions()


def test_a_fake_memory_result_cannot_be_labeled_mem0(tmp_path: Path) -> None:
    path = tmp_path / "answers.jsonl"
    try:
        showcase.record_answer(
            path,
            {"id": "q", "arm": "mem0-separated", "team": "sales", "answer": "x", "engine": "fake-separated"},
        )
    except bench.BenchError:
        pass
    else:
        raise AssertionError("a fake result was labeled mem0")
    try:
        showcase.render_report(
            [{"size": 1000, "arm": "mem0-shared", "task": "recall", "correct": False, "leak": False, "engine": "fake-separated"}]
        )
    except bench.BenchError:
        pass
    else:
        raise AssertionError("a report labeled a fake result as mem0")
    try:
        showcase.grade_packet([], {"mem0-separated": {}}, seed=1, engines={"mem0-separated": "fake-separated"})
    except bench.BenchError:
        pass
    else:
        raise AssertionError("a packet labeled a fake result as mem0")
    assert showcase.record_answer(path, {"id": "q", "arm": "fake-separated", "team": "sales", "answer": "x", "engine": "fake-separated"})


def test_mem0_search_reopens_the_saved_configuration(tmp_path: Path) -> None:
    run = tmp_path / "run"
    bench.prepare_run_dir(run, create=True)
    separated = {"vector_store": {"config": {"path": "separated"}}}
    shared = {"vector_store": {"config": {"path": "shared"}}, "user_id": "shared"}
    payload = {
        "engine": "mem0",
        "separated": {"config": separated},
        "shared": {"config": shared, "user_id": "shared"},
    }
    (run / "memory.json").write_text(json.dumps(payload), encoding="utf-8")
    opened: list[object] = []

    class Probe:
        def search(self, query: str, *, user_id: str) -> dict[str, list[dict[str, str]]]:
            opened.append(user_id)
            return {"results": [{"memory": query + " " + user_id}]}

    def opener(config: dict[str, object]) -> Probe:
        opened.append(config)
        return Probe()

    separated_text = showcase.mem0_search(run, team="sales", query="harbor", setup="separated", opener=opener)
    shared_text = showcase.mem0_search(run, team="sales", query="harbor", setup="shared", opener=opener)
    assert separated_text == "harbor sales"
    assert shared_text == "harbor shared"
    assert opened[0] == separated
    assert opened[2] == shared
    fake = tmp_path / "fake"
    bench.prepare_run_dir(fake, create=True)
    (fake / "memory.json").write_text(json.dumps({"engine": "fake-separated", "counts": {"sales": 1}}), encoding="utf-8")
    try:
        showcase.mem0_search(fake, team="sales", query="harbor")
    except bench.BenchError:
        pass
    else:
        raise AssertionError("fake memory was searched as mem0")


def test_the_report_names_the_development_set_and_who_enforced_mem0() -> None:
    rows = [
        {"size": 1000, "arm": "mem0-separated", "task": "recall", "correct": True, "leak": False},
        {"size": 1000, "arm": "mem0-shared", "task": "recall", "correct": False, "leak": True},
    ]
    text = showcase.render_report(rows)
    assert text.startswith("These questions are a development set")
    assert "showcase-heldout" in text
    assert "mem0 enforced nothing" in text
    assert "Nothing separated the teams" in text
    held = showcase.render_report(rows, heldout=True)
    assert "development set" not in held
    assert "mem0 enforced nothing" in held


def test_import_posts_note_text_to_the_configured_endpoint(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            seen["path"] = self.path
            seen["body"] = body.decode("utf-8")
            payload = json.dumps({"data": [{"index": 0, "embedding": [0.25, 0.5]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        corpus = tmp_path / "notes"
        _note(corpus, "sales/note.md", "sales", "internal", "professional", "quarry note for the embedder")
        settings = showcase.embedding_settings(f"http://127.0.0.1:{port}", "nomic-embed-text:v1.5")
        showcase.import_corpus(tmp_path / "run", corpus, embeddings=settings)
        assert seen.get("path") == "/v1/embeddings"
        assert "quarry note" in seen.get("body", "")
        run = tmp_path / "recall"
        db_path = showcase.import_corpus(run, corpus)
        keys = showcase._create_keys(db_path)
        (run / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
        (run / "showcase.json").write_text(
            json.dumps({"embeddings": "on", "embedding_env": settings}),
            encoding="utf-8",
        )
        seen.clear()
        text = showcase.alice_search(run, team="leadership", query="quarry")
    finally:
        server.shutdown()
    assert seen["path"] == "/v1/embeddings"
    assert "quarry" in seen["body"]
    assert "quarry note" in text


def test_the_team_table_in_the_harness_matches_a_live_run(tmp_path: Path) -> None:
    table = showcase.observe_team_limits(tmp_path)
    text = (bench.REPO_ROOT / "docs/benchmarks/showcase/harness.md").read_text(encoding="utf-8")
    assert table.strip() in text
    assert "memory_proposal_agent" in table
    assert "memory.commit" in table


def test_showcase_live_refuses_a_host_that_is_not_loopback(tmp_path: Path) -> None:
    code = bench.main(
        ["showcase-live", "--run-dir", str(tmp_path / "run"), "--notes", "20", "--endpoint", "https://example.com"]
    )
    assert code != 0


def test_each_command_in_the_harness_note_runs(tmp_path: Path) -> None:
    """Subprocess, with the repository root not on PYTHONPATH, as the note writes the command."""

    note = (bench.REPO_ROOT / "docs/benchmarks/showcase/harness.md").read_text(encoding="utf-8")
    fence = note.split("```")[1]
    commands = [line.strip() for line in fence.splitlines() if line.strip().startswith("python ")]
    assert len(commands) >= 8
    folder = tmp_path / "company"
    run = tmp_path / "run"
    questions = tmp_path / "questions.json"
    answers = tmp_path / "answers.json"
    packet = tmp_path / "packet.json"
    key = tmp_path / "key.json"
    grades = tmp_path / "grades.json"
    grades.write_text(
        json.dumps([{"size": 1000, "arm": "alice", "task": "recall", "correct": True, "leak": False}]),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(bench.REPO_ROOT / "apps" / "api" / "src")
    question_id = ""
    for line in commands:
        rendered = (
            line.replace("DIR", str(folder))
            .replace("RUN", str(run))
            .replace("questions.json", str(questions))
            .replace("answers.json", str(answers))
            .replace("packet.json", str(packet))
            .replace("key.json", str(key))
            .replace("grades.json", str(grades))
            .replace("FILE", str(questions))
            .replace("ID", question_id or "1000-recall-01")
            .replace('"..."', '"contract"')
        )
        argv = shlex.split(rendered)
        if argv[2:3] == ["showcase-packet"] and not answers.exists():
            document = json.loads(questions.read_text(encoding="utf-8"))
            question_id = document["questions"][0]["id"]
            by_id = {item["id"]: "QF-C-2041" for item in document["questions"]}
            other = {item["id"]: "other" for item in document["questions"]}
            answers.write_text(json.dumps({"alice": by_id, "grep": other}), encoding="utf-8")
            rendered = rendered.replace("1000-recall-01", question_id)
            argv = shlex.split(rendered)
        completed = subprocess.run(
            [sys.executable, *argv[1:]],
            cwd=bench.REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert completed.returncode == 0, rendered + "\n" + completed.stderr[-2000:]
        if "showcase-questions" in rendered:
            document = json.loads(questions.read_text(encoding="utf-8"))
            question_id = document["questions"][0]["id"]
