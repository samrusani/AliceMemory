"""The showcase bench: questions, arms, a blind packet, and team keys.

These tests use invented notes. They call no paid model and no network, apart
from a loopback embeddings server started in one test.

Mutation: set SUPPORT_PROFILE to admin_agent. The support key would then read
the HR pay record, and test_a_support_key_does_not_read_hr_or_a_draft_price fails.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
from alicebot_api.vnext_embeddings import OpenAICompatibleEmbeddingProvider

from tests.fixtures.showcase_company.company import ABSENT


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
    assert showcase.record_answer(path, {"id": questions[0]["id"], "answer": "first"}) is True
    assert showcase.record_answer(path, {"id": questions[0]["id"], "answer": "second"}) is False
    assert showcase.load_done(path)[questions[0]["id"]]["answer"] == "first"
    batch = showcase.pending(questions, showcase.load_done(path), 2)
    assert [item["id"] for item in batch] == [questions[1]["id"], questions[2]["id"]]


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
    assert showcase.SUPPORT_PROFILE == "read_only_agent"
    assert showcase.team_can_read("support", domain="legal", sensitivity="confidential") is False
    assert showcase.team_can_read("support", domain="financial", sensitivity="private") is False
    assert showcase.team_can_read("sales", domain="financial", sensitivity="private") is True
    assert showcase.team_can_read("sales", domain="legal", sensitivity="confidential") is False
    assert showcase.team_can_read("leadership", domain="legal", sensitivity="confidential") is True
    memory = showcase.FakeMemory()
    memory.add("compensation is 128400 ledger units", scope="leadership")
    memory.add("Draft list price 910 ledger", scope="sales")
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
