"""A showcase run of search quality ``vectors`` really uses the chunk vectors, and cannot fall back to passage mode unseen.

The harness used to record ``vectors`` nowhere and embed nothing: it posted note text to the embeddings endpoint, threw the
vectors away, and ran recall with no endpoint, so a run built "with embeddings" answered from the words alone. These
tests start a tiny embeddings server on 127.0.0.1 (``/v1/embeddings`` and Ollama's ``/api/tags``) and check the parts of
the fix against the real product code: the build embeds through the product and fails unless nothing is pending, every
search of the Alice arm checks the vault and the environment again, every recall records the status of the vector stage,
and the text an answering agent reads is the text it read before. A passage run and an off run are checked to be what
they were. No test calls the network beyond that loopback server.

Each test names the edit that must make it fail. Four of them are replayed by
``scripts/derived_label_mutations.json`` (the cases named ``bench-vectors-*``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
from alicebot_api.source_chunk_vector_search import reset_breaker
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import clear_registry
from tests.unit.test_showcase_bench import _hidden_questions, _note

DIMENSIONS = 16
MODEL = "nomic-embed-text:v1.5"
DIGEST = "fake-digest-0001"
REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "tests" / "fixtures" / "search_quality" / "corpus"


def bag_vector(text: str) -> list[float]:
    """A unit vector from the words of a text: texts that share words are near each other."""

    values = [0.0] * DIMENSIONS
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        values[int(hashlib.sha1(token.encode(), usedforsecurity=False).hexdigest(), 16) % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


class FakeEmbeddingsServer:
    """``/v1/embeddings`` and ``/api/tags`` on 127.0.0.1, in this process.

    ``fail_first`` makes the first N chunk requests answer 500, which leaves chunks pending after an import.
    ``fail_queries`` makes every query request (one text, the query prefix) answer 500. ``texts`` is every text sent.
    """

    def __init__(self, *, digest: str = DIGEST, fail_first: int = 0, fail_queries: bool = False) -> None:
        self.digest = digest
        self.fail_first = fail_first
        self.fail_queries = fail_queries
        self.texts: list[str] = []
        self.chunk_requests = 0
        self.failed_requests = 0
        self.tag_requests = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/tags":
                    self._send(404, {})
                    return
                with outer._lock:
                    outer.tag_requests += 1
                self._send(200, {"models": [{"name": MODEL, "model": MODEL, "digest": outer.digest}]})

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path != "/v1/embeddings":
                    self._send(404, {})
                    return
                inputs = json.loads(raw)["input"]
                is_query = len(inputs) == 1 and inputs[0].startswith("search_query: ")
                with outer._lock:
                    outer.texts.extend(inputs)
                    fail = outer.fail_queries if is_query else outer.chunk_requests < outer.fail_first
                    if not is_query:
                        outer.chunk_requests += 1
                    if fail:
                        outer.failed_requests += 1
                if fail:
                    self._send(500, {"error": "scripted failure"})
                    return
                self._send(200, {"data": [{"index": i, "embedding": bag_vector(t)} for i, t in enumerate(inputs)]})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self) -> FakeEmbeddingsServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ALICE variable of the parent, no proxy, and empty caches, before and after each test."""

    for name in [name for name in os.environ if name.startswith("ALICE")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    clear_registry()
    reset_breaker()
    yield
    clear_registry()
    reset_breaker()


def _write_notes(folder: Path) -> None:
    """Six notes, each one chunk. Their front matter is part of the chunk, so even the one-word note is long enough to embed."""

    texts = {
        "shared/harbor.md": "The shared harbor note mentions harborlatchtoken and the latch on the east pier.",
        "shared/kiln.md": "The kiln schedule lists amberkilntoken for the Tuesday firing of the blue glaze.",
        "shared/orchard.md": "Orchard pruning happens in late winter, and the ladder store keys are with the foreman.",
        "shared/ledger.md": "The ledger summary for the third quarter mentions quartzledgertoken and nothing else.",
        "shared/rota.md": "The weekend rota puts two people on the loading dock and one on the front desk.",
        "shared/short.md": "ok",
    }
    for relative, body in texts.items():
        _note(folder, relative, "sales", "internal", "professional", body)


def _build_args(tmp_path: Path, server: FakeEmbeddingsServer, *extra: str) -> list[str]:
    notes = tmp_path / "notes"
    if not notes.exists():
        _write_notes(notes)
    questions = tmp_path / "questions.json"
    if not questions.exists():
        _hidden_questions(questions)
    return [
        "showcase-heldout",
        "--notes",
        str(notes),
        "--questions",
        str(questions),
        "--run-dir",
        str(tmp_path / "run"),
        "--endpoint",
        server.endpoint,
        *extra,
    ]


def _vectors_run(tmp_path: Path, server: FakeEmbeddingsServer, capsys: pytest.CaptureFixture[str]) -> Path:
    code = bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on"))
    assert code == 0, capsys.readouterr().err
    capsys.readouterr()
    return tmp_path / "run"


def _manifest(run: Path) -> dict[str, Any]:
    return json.loads((run / "showcase.json").read_text(encoding="utf-8"))


def _vault(run: Path) -> Path:
    return bench.vault_path(run / bench.VAULT_DIRNAME)


def _counts(db_path: Path) -> tuple[int, int]:
    """``(chunks, stored vectors)`` of a vault."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        chunks = int(conn.execute("SELECT COUNT(*) FROM source_chunks").fetchone()[0])
        vectors = int(conn.execute("SELECT COUNT(*) FROM source_chunk_embeddings").fetchone()[0])
    finally:
        conn.close()
    return chunks, vectors


def _search(run: Path, *extra: str, query: str = "harborlatchtoken", team: str = "leadership") -> int:
    return bench.main(
        ["showcase-search", "--run-dir", str(run), "--arm", "alice", "--team", team, "--query", query, *extra]
    )


def test_a_vectors_build_embeds_every_chunk_and_records_the_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build embeds through the product: every chunk the product sends has a stored vector, and the record says so.

    Mutation: build with the opt-in left out (the import embeds nothing), record ``ranking_mode`` as ``passage``, or
    leave the digest, the width or the signature out of the block.
    """

    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        manifest = _manifest(run)
        block = manifest["chunk_vectors"]
        chunks, stored = _counts(_vault(run))
        assert (manifest["search_quality"], manifest["ranking_mode"], manifest["embeddings"]) == (
            "vectors",
            "vectors",
            "on",
        )
        assert manifest["embedding_env"] == {
            "ALICE_EMBEDDINGS_BASE_URL": server.endpoint + "/v1",
            "ALICE_EMBEDDINGS_MODEL": MODEL,
        }
        assert chunks == 6
        assert block["chunks_embedded"] == stored == 6
        assert block["skipped"] == {} and block["skipped_total"] == 0
        assert (block["pending"], block["stale"]) == (0, 0)
        assert (block["model"], block["model_digest"], block["dimensions"]) == (MODEL, DIGEST, DIMENSIONS)
        assert block["signature"]["model"] == MODEL
        assert block["signature"]["provider"] == "openai_compatible"
        assert {"endpoint", "signature_version", "header_version", "prefix_version"} <= set(block["signature"])
        assert block["backfill_passes"] >= 1
        # The product sent each embedded chunk once, behind the model's document prefix, and the backfill sent nothing again.
        assert len(server.texts) == stored
        assert all(text.startswith("search_document: ") for text in server.texts)
        assert any("harborlatchtoken" in text for text in server.texts)
    assert not [name for name in os.environ if name.startswith("ALICE")], "the build put its variables back"


def test_the_backfill_completes_what_the_import_left_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two chunk requests fail while the notes are imported. The product's backfill embeds those chunks afterwards.

    Mutation: skip the backfill in ``embed_vault_chunks`` (the ``_run_backfill`` call). The two chunks stay pending
    and the build is refused, so the build assertion below fails.
    """

    with FakeEmbeddingsServer(fail_first=2) as server:
        run = _vectors_run(tmp_path, server, capsys)
        block = _manifest(run)["chunk_vectors"]
        assert server.failed_requests == 2
        assert _counts(_vault(run)) == (6, 6)
        assert (block["chunks_embedded"], block["pending"]) == (6, 0)


def test_a_vectors_build_fails_when_a_chunk_stays_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An endpoint that refuses every chunk leaves everything pending. The build says so in one line and records nothing.

    Mutation: let the build go on with chunks pending (drop the ``pending`` check in ``embed_vault_chunks``).
    """

    with FakeEmbeddingsServer(fail_first=10**6) as server:
        code = bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on"))
    err = capsys.readouterr().err
    assert code == bench.EXIT_REFUSED
    assert err.count("\n") == 1 and err.startswith("refused: the chunk vectors are incomplete")
    assert "6 chunks are still pending a vector" in err
    assert not (tmp_path / "run" / "showcase.json").exists()


def test_skips_other_than_the_documented_ones_fail_the_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The product skips a chunk of credential text, of links only, or too short to read. Any other skip is named and refused.

    Mutation: accept every reason, or refuse ``too_short``.
    """

    real = bench.chunk_vector_state
    skipped: dict[str, int] = {}

    def with_skips(db_path: Path) -> dict[str, Any]:
        return {**real(db_path), "skipped": dict(skipped)}

    monkeypatch.setattr(bench, "chunk_vector_state", with_skips)
    with FakeEmbeddingsServer() as server:
        skipped.update({"too_short": 2, "source_retired": 3, "mystery": 1})
        code = bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on"))
        err = capsys.readouterr().err
        assert code == bench.EXIT_REFUSED and err.count("\n") == 1
        assert "mystery 1" in err and "source_retired 3" in err and "too_short" not in err
        skipped.clear()
        skipped.update({"credential": 1, "link_only": 1, "too_short": 1})
        assert bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on")) == 0
    block = _manifest(tmp_path / "run")["chunk_vectors"]
    assert block["skipped"] == {"credential": 1, "link_only": 1, "too_short": 1} and block["skipped_total"] == 3


def test_a_vectors_build_needs_embeddings_on_and_an_endpoint_on_this_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--embeddings on`` there is nothing to rank by, and a remote host is never sent a note.

    Mutation: build a vectors run with embeddings off, or accept a host that is not loopback.
    """

    with FakeEmbeddingsServer() as server:
        off = bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "off"))
        assert off == bench.EXIT_REFUSED and "--embeddings on" in capsys.readouterr().err
        remote = bench.main(
            _build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on", "--endpoint", "https://example.com")
        )
        assert remote == bench.EXIT_REFUSED and "loopback" in capsys.readouterr().err
        assert server.texts == [] and not (tmp_path / "run").exists()


@pytest.mark.parametrize("damage", ["pending", "stale", "recorded-more", "recorded-fewer", "digest"])
def test_a_vectors_search_refuses_when_anything_is_pending_or_the_counts_differ(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """Every search of the Alice arm checks the vault against the build. A refusal is one line and spends no search.

    Mutation: remove the readiness gate (the ``require_vectors_ready`` call in ``showcase-search``), or compare only
    the pending count and not the stored count.
    """

    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        assert _search(run, "--search-quality", "vectors") == 0
        capsys.readouterr()
        if damage in {"pending", "stale"}:
            conn = sqlite3.connect(_vault(run))
            try:
                if damage == "pending":
                    conn.execute("DELETE FROM source_chunk_embeddings WHERE rowid = (SELECT MIN(rowid) FROM source_chunk_embeddings)")
                else:
                    conn.execute("UPDATE source_chunk_embeddings SET model = 'another-model' WHERE rowid = 1")
                conn.commit()
            finally:
                conn.close()
        elif damage == "digest":
            server.digest = "another-digest"
        else:
            manifest = _manifest(run)
            manifest["chunk_vectors"]["chunks_embedded"] += 1 if damage == "recorded-more" else -1
            (run / "showcase.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        assert _search(run, "--search-quality", "vectors") == bench.EXIT_REFUSED
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and err.startswith("refused: this vectors run is not ready")
        assert {"pending": "pending", "stale": "stale", "digest": "digest"}.get(damage, "build recorded") in err
        assert (run / "state" / "alice-leadership" / "search.count").read_text(encoding="utf-8") == "1"
        # No arm is searched in a run whose vectors are not whole.
        grep = ["showcase-search", "--run-dir", str(run), "--arm", "grep", "--pattern", "harbor", "--search-quality", "vectors"]
        assert bench.main(grep) == bench.EXIT_REFUSED
        assert capsys.readouterr().err.startswith("refused: this vectors run is not ready")
        assert not (run / "state" / "grep-sales").exists()


def test_a_vectors_search_carries_the_embedding_environment_and_no_other_run_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in, the endpoint and the model reach a vectors run and nothing else that starts with ALICE does.

    The parent holds an agent key, an API key, another endpoint and the opt-in. A passage run sees the search quality only.

    Mutation: leave the embedding variables out of ``arm_environment`` for a vectors run (the search is then refused),
    or let them through for any other quality.
    """

    seen: list[dict[str, str]] = []

    def spy(_run_dir: Path, *, team: str, query: str) -> str:
        del team, query
        seen.append({name: value for name, value in os.environ.items() if name.startswith("ALICE")})
        return "harborlatchtoken"

    monkeypatch.setattr(showcase, "fake_separated_search", spy)
    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        passage = tmp_path / "passage"
        built = bench.main(
            [
                "showcase-heldout",
                *["--notes", str(tmp_path / "notes"), "--questions", str(tmp_path / "questions.json")],
                *["--run-dir", str(passage), "--endpoint", server.endpoint, "--search-quality", "passage"],
            ]
        )
        assert built == 0
        capsys.readouterr()
        monkeypatch.setenv("ALICE_AGENT_API_KEY", "a-parent-key")
        monkeypatch.setenv("ALICE_EMBEDDINGS_API_KEY", "a-parent-api-key")
        monkeypatch.setenv("ALICE_EMBEDDINGS_BASE_URL", "http://example.com/v1")
        monkeypatch.setenv("ALICE_SOURCE_EMBEDDINGS", "off")
        for target, quality in ((run, "vectors"), (passage, "passage")):
            code = bench.main(
                ["showcase-search", "--run-dir", str(target), "--arm", "fake-separated", "--search-quality", quality]
            )
            assert code == 0
    vectors_env, passage_env = seen
    assert vectors_env == {
        "ALICE_SEARCH_QUALITY": "vectors",
        "ALICE_SOURCE_EMBEDDINGS": "on",
        "ALICE_EMBEDDINGS_BASE_URL": server.endpoint + "/v1",
        "ALICE_EMBEDDINGS_MODEL": MODEL,
    }
    assert passage_env == {"ALICE_SEARCH_QUALITY": "passage"}
    assert os.environ["ALICE_EMBEDDINGS_BASE_URL"] == "http://example.com/v1", "the parent's value came back"


def test_the_environment_of_a_run_is_its_quality_and_for_vectors_its_endpoint() -> None:
    """The allowlist, without a run: what ``arm_environment`` lets through, and what a vectors run may be given.

    Mutation: let the settings through for a run of another quality, or accept more than the endpoint and the model.
    """

    settings = {"ALICE_EMBEDDINGS_BASE_URL": "http://127.0.0.1:11434/v1", "ALICE_EMBEDDINGS_MODEL": MODEL}
    assert bench.arm_environment(None) == {}
    assert bench.arm_environment("off") == {"ALICE_SEARCH_QUALITY": "off"}
    assert bench.arm_environment("passage") == {"ALICE_SEARCH_QUALITY": "passage"}
    assert bench.arm_environment("vectors") == {"ALICE_SEARCH_QUALITY": "vectors"}
    assert bench.arm_environment("vectors", vector_settings=settings) == {
        "ALICE_SEARCH_QUALITY": "vectors",
        "ALICE_SOURCE_EMBEDDINGS": "on",
        **settings,
    }
    for quality in (None, "off", "passage", "on"):
        with pytest.raises(bench.BenchError):
            bench.arm_environment(quality, vector_settings=settings)
    with pytest.raises(bench.BenchError):
        bench.vector_environment({**settings, "ALICE_EMBEDDINGS_API_KEY": "k"})
    with pytest.raises(bench.BenchError):
        bench.vector_environment({**settings, "ALICE_EMBEDDINGS_BASE_URL": "https://example.com/v1"})
    parent = {"PATH": "/usr/bin", "ALICE_AGENT_API_KEY": "k", "ALICE_SOURCE_EMBEDDINGS": "off", "ALICE_EMBEDDINGS_API_KEY": "k"}
    carried = bench.scrub_environment(parent, allow=bench.arm_environment("vectors", vector_settings=settings))
    assert sorted(name for name in carried if name.startswith("ALICE")) == [
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_SEARCH_QUALITY",
        "ALICE_SOURCE_EMBEDDINGS",
    ]
    assert carried["ALICE_SOURCE_EMBEDDINGS"] == "on" and carried["PATH"] == "/usr/bin"


def _plain_text(run: Path, team: str, query: str, quality: str, settings: dict[str, str] | None = None) -> str:
    """What the Alice arm returned before the trace existed: a recall with no ``debug``, with the echoed query removed."""

    from alicebot_api.vnext_agent_control import _profile_sensitivity

    keys = json.loads((run / "keys.json").read_text(encoding="utf-8"))
    identity = showcase._identity(_vault(run), keys[team])
    with bench.scoped_environment(bench.arm_environment(quality, vector_settings=settings)):
        session = bench.McpSession(_vault(run), agent_identity=identity)
        arguments = bench.alice_arm_arguments(query)
        arguments["sensitivity_allowed"] = list(_profile_sensitivity(showcase.TEAM_PROFILES[team]))
        is_error, text = session.call(bench.RECALL_TOOL, arguments)
    assert not is_error
    return showcase.without_echoed_query(text)


QUERIES = (
    "harborlatchtoken",
    'what is the "blue glaze" firing on Tuesday?',
    "orchard ladder store keys",
    "quartzledgertoken third quarter",
    "who covers the loading dock at the weekend",
    "zzzz no word of this is in any note",
)


@pytest.mark.parametrize("query", QUERIES)
def test_a_vectors_search_records_its_status_and_the_agent_text_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    """The status goes to the state folder. The text on stdout, and its hash in the log, are those of a call without the trace.

    Mutation: return the trace with the text (leave ``retrieval`` in), drop the status file, or record a status
    other than the stage's own.
    """

    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        settings = _manifest(run)["embedding_env"]
        assert _search(run, "--search-quality", "vectors", query=query) == 0
        shown = capsys.readouterr().out
        plain = _plain_text(run, "leadership", query, "vectors", settings)
        assert shown == plain + "\n"
        assert "retrieval" not in shown and "debug" not in shown
        state = run / "state" / "alice-leadership"
        statuses = [json.loads(line) for line in (state / bench.VECTOR_STATUS_FILENAME).read_text(encoding="utf-8").splitlines()]
        assert len(statuses) == 1
        record = statuses[0]
        assert (record["n"], record["arm"], record["status"], record["used"]) == (1, "alice", "enabled", True)
        assert record["seconds"] >= 0
        assert {"readable_chunks", "stale_chunks", "discovered_in_head", "reordered_slots", "filler_picks"} <= set(record)
        assert record["readable_chunks"] == 6
        log = [json.loads(line) for line in (state / "search_log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert set(log[0]) == {"n", "at", "arm", "input", "status", "bytes", "truncated", "sha1"}
        assert log[0]["sha1"] == hashlib.sha1(plain.encode("utf-8"), usedforsecurity=False).hexdigest()
        # The query itself went to the endpoint, behind the query prefix.
        assert "search_query: " + query in server.texts


def test_a_search_that_did_not_use_vectors_says_so_and_answers_as_passage_mode_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The endpoint refuses the query: the status names that, ``used`` is false, and the text is the passage text.

    A call made without the vectors environment is refused outright and is never recalled as passage mode.

    Mutation: record ``enabled`` whatever the stage said, or recall a vectors run without checking the environment.
    """

    query = "harborlatchtoken"
    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        settings = _manifest(run)["embedding_env"]
        server.fail_queries = True
        with pytest.raises(bench.BenchError, match="not applied"):
            showcase.alice_search_with_status(run, team="leadership", query=query)
        with bench.scoped_environment(bench.arm_environment("vectors", vector_settings=settings)):
            text, status = showcase.alice_search_with_status(run, team="leadership", query=query)
        assert status is not None
        assert (status["status"], status["used"]) == ("skipped: query embedding failed", False)
        assert text == _plain_text(run, "leadership", query, "passage")


def test_the_ranking_mode_of_vectors_is_vectors_and_never_passage(monkeypatch: pytest.MonkeyPatch) -> None:
    """``vectors`` resolves to ``vectors``. A checkout that would rank it as another mode is refused, not recorded.

    Mutation: return ``passage`` for a run of ``vectors`` (``_checked_ranking_mode``).
    """

    from alicebot_api.mcp import retrieval as recall_edge
    from alicebot_api.source_ranking import SourceRanking

    assert bench.resolved_ranking_mode("vectors") == "vectors"
    assert bench.resolved_ranking_mode("passage") == "passage"
    monkeypatch.setattr(recall_edge, "recall_source_ranking", lambda _store, _identity: SourceRanking.passage())
    with pytest.raises(bench.BenchError, match="resolved to the passage ranking"):
        bench.resolved_ranking_mode("vectors")
    monkeypatch.setattr(recall_edge, "recall_source_ranking", lambda _store, _identity: SourceRanking.vectors())
    with pytest.raises(bench.BenchError, match="resolved to the vectors ranking"):
        bench.resolved_ranking_mode("passage")
    monkeypatch.delattr(recall_edge, "recall_source_ranking")
    with pytest.raises(bench.BenchError, match="resolved to the document ranking"):
        bench.resolved_ranking_mode("vectors")
    assert bench.resolved_ranking_mode("passage") == "document"


@pytest.mark.parametrize(
    ("flags", "recorded", "mode"),
    [
        pytest.param([], "unset", "passage", id="unset"),
        pytest.param(["--search-quality", "off"], "off", "document", id="off"),
        pytest.param(["--search-quality", "passage"], "passage", "passage", id="passage"),
        pytest.param(["--search-quality", "passage", "--embeddings", "on"], "passage", "passage", id="passage-embeddings-on"),
    ],
)
def test_a_passage_or_off_run_is_what_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str], recorded: str, mode: str
) -> None:
    """No vector is made, no note or chunk is sent anywhere, the manifest has no new key, and the text is the recall text.

    Mutation: embed at build time for a run that is not ``vectors`` (send the notes, write a vector table), add a
    key to the manifest of such a run, or write a status file for it.
    """

    with FakeEmbeddingsServer() as server:
        assert bench.main(_build_args(tmp_path, server, *flags)) == 0
        capsys.readouterr()
        run = tmp_path / "run"
        assert server.texts == [] and server.tag_requests == 0
        manifest = _manifest(run)
        assert set(manifest) == {
            "schema",
            "corpus",
            "notes",
            "models",
            "embedding_env",
            "embeddings",
            "extractor",
            "teams",
            "capture",
            "search_quality",
            "ranking_mode",
        }
        assert (manifest["search_quality"], manifest["ranking_mode"]) == (recorded, mode)
        conn = sqlite3.connect(_vault(run))
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "source_chunk_embeddings" not in tables
        quality = [] if recorded == "unset" else ["--search-quality", recorded]
        assert _search(run, *quality) == 0
        shown = capsys.readouterr().out
        settings = manifest["embedding_env"] if "--embeddings" in flags else None
        env = bench.arm_environment(None if recorded == "unset" else recorded)
        env.update(settings or {})
        with bench.scoped_environment(env):
            plain = _plain_text_in_scope(run, "leadership", "harborlatchtoken")
        assert shown == plain + "\n"
        assert not list((run / "state").glob(f"*/{bench.VECTOR_STATUS_FILENAME}"))


def _plain_text_in_scope(run: Path, team: str, query: str) -> str:
    from alicebot_api.vnext_agent_control import _profile_sensitivity

    keys = json.loads((run / "keys.json").read_text(encoding="utf-8"))
    session = bench.McpSession(_vault(run), agent_identity=showcase._identity(_vault(run), keys[team]))
    arguments = bench.alice_arm_arguments(query)
    arguments["sensitivity_allowed"] = list(_profile_sensitivity(showcase.TEAM_PROFILES[team]))
    is_error, text = session.call(bench.RECALL_TOOL, arguments)
    assert not is_error
    return showcase.without_echoed_query(text)


def test_the_report_counts_how_many_searches_used_vectors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two searches, one of which the endpoint could not serve: the report says 2 and 1, and names the status.

    A run of another quality gets no such lines.

    Mutation: count every recorded search as one that used vectors, or print the lines for a passage run.
    """

    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        assert _search(run, "--search-quality", "vectors") == 0
        server.fail_queries = True
        assert _search(run, "--search-quality", "vectors", team="sales") == 0
        capsys.readouterr()
        grades = tmp_path / "grades.json"
        row = {"corpus": "heldout", "notes": 6, "arm": "alice", "task": "recall", "correct": True, "leak": False}
        grades.write_text(json.dumps([row]), encoding="utf-8")
        assert bench.main(["showcase-report", "--grades", str(grades), "--run-dir", str(run), "--heldout"]) == 0
        report = capsys.readouterr().out
        assert "vector searches 2, used vectors 1" in report
        assert "  enabled: 1" in report and "  skipped: query embedding failed: 1" in report
        passage = tmp_path / "passage"
        assert bench.main(_build_args(tmp_path, server, "--search-quality", "passage", "--run-dir", str(passage))) == 0
        capsys.readouterr()
        assert bench.main(["showcase-report", "--grades", str(grades), "--run-dir", str(passage), "--heldout"]) == 0
        assert "vector searches" not in capsys.readouterr().out


@pytest.mark.parametrize("team", showcase.TEAMS)
def test_every_team_key_reports_the_status_of_the_vector_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str
) -> None:
    """The four keys differ in what they may read, and each one's recall still carries a stage status.

    Mutation: read the status from a place only an unlimited key has.
    """

    with FakeEmbeddingsServer() as server:
        run = _vectors_run(tmp_path, server, capsys)
        settings = _manifest(run)["embedding_env"]
        with bench.scoped_environment(bench.arm_environment("vectors", vector_settings=settings)):
            _text, status = showcase.alice_search_with_status(run, team=team, query="harborlatchtoken")
        assert status is not None and (status["status"], status["used"]) == ("enabled", True)


def test_the_build_prints_what_it_embedded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with FakeEmbeddingsServer() as server:
        assert bench.main(_build_args(tmp_path, server, "--search-quality", "vectors", "--embeddings", "on")) == 0
        out = capsys.readouterr().out
    assert f"chunk vectors embedded 6, pending 0, skipped 0, model {MODEL}, digest {DIGEST}" in out


def test_the_harness_note_says_what_a_vectors_run_embeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The note names the opt-in, the backfill, the record and the status file, every key of the record, and no longer
    says that the import posts note text.

    Mutation: restore the sentence that the import posts the note text to the endpoint, or leave a key of the
    ``chunk_vectors`` block out of the note.
    """

    note = (REPO_ROOT / "docs" / "benchmarks" / "showcase" / "harness.md").read_text(encoding="utf-8")
    with FakeEmbeddingsServer() as server:
        block = _manifest(_vectors_run(tmp_path, server, capsys))["chunk_vectors"]
    for key in block:
        assert f"`{key}`" in note, key
    for needle in (
        "ALICE_SOURCE_EMBEDDINGS=on",
        "reindex-embeddings --sources",
        "vector_status.jsonl",
        "`off`, `passage`, `vectors` or `on`",
        "`document`, `passage` or `vectors`",
        "search_document: ",
        "/api/tags",
    ):
        assert needle in note, needle
    assert "the import posts the note text" not in note
    assert "—" not in note and "–" not in note


def test_the_alice_bench_commands_build_and_read_a_vectors_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``build`` embeds through the product. ``recall``, ``batch``, ``search`` and ``fingerprint`` check the vault, record
    each call's status and return the text a call without the trace returns. A vault is read only as the quality it was
    built for.

    Mutation: leave the trace in the text of a recall, record ``ranking_mode`` as ``passage``, or skip the readiness
    check of ``_context``.
    """

    questions = REPO_ROOT / "tests" / "fixtures" / "search_quality" / "questions.json"
    flags = ["--search-quality", "vectors"]
    run = tmp_path / "bench"
    with FakeEmbeddingsServer() as server:
        built = bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), *flags, "--embeddings", "on", "--endpoint", server.endpoint])
        assert built == 0
        printed = capsys.readouterr().out
        manifest = json.loads((run / bench.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        block = manifest["chunk_vectors"]
        assert (manifest["build"]["search_quality"], manifest["build"]["ranking_mode"]) == ("vectors", "vectors")
        assert block["chunks_embedded"] + block["skipped_total"] == manifest["chunks"] and block["pending"] == 0
        assert block["model_digest"] == DIGEST and manifest["vault_row_counts"]["source_chunk_embeddings"] == block["chunks_embedded"]
        assert f"chunk vectors embedded {block['chunks_embedded']}" in printed
        settings = manifest["embedding_env"]
        query = "Who has the spare key to the brass cabinet?"
        with bench.scoped_environment(bench.arm_environment("vectors", vector_settings=settings)):
            plain = bench.McpSession(bench.vault_path(run / bench.VAULT_DIRNAME)).recall(query)

        assert bench.main(["recall", "--run-dir", str(run), "--query", query, *flags]) == 0
        captured = capsys.readouterr()
        assert captured.out == plain + "\n" and captured.err == "vector stage: enabled\n"

        out = tmp_path / "outputs.json"
        assert bench.main(["batch", "--run-dir", str(run), "--questions", str(questions), "--out", str(out), *flags]) == 0
        document = json.loads(out.read_text(encoding="utf-8"))
        qset = bench.load_questions(questions)
        assert document["vector_stage"] == {
            "calls": 2 * len(qset.questions),
            "used": 2 * len(qset.questions),
            "statuses": {"enabled": 2 * len(qset.questions)},
        }
        assert all("retrieval" not in text for by_variant in document["outputs"].values() for text in by_variant.values())
        assert document["outputs"][qset.questions[0].id]["verbatim"] is not None
        fp = document["fingerprint"]
        assert (fp["search_quality"], fp["ranking_mode"], fp["chunk_vectors"]) == ("vectors", "vectors", block)

        capsys.readouterr()
        assert bench.main(["fingerprint", "--run-dir", str(run), *flags]) == 0
        assert json.loads(capsys.readouterr().out)["ranking_mode"] == "vectors"

        assert bench.main(["search", "--run-dir", str(run), "--arm", "alice", "--query", query, *flags]) == 0
        assert capsys.readouterr().out == plain + "\n"
        statuses = (run / "state" / "default" / bench.VECTOR_STATUS_FILENAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["status"] for line in statuses] == ["enabled"]

        for other in ([], ["--search-quality", "passage"]):
            assert bench.main(["recall", "--run-dir", str(run), "--query", query, *other]) == bench.EXIT_REFUSED
            assert "built for search quality vectors" in capsys.readouterr().err

        conn = sqlite3.connect(bench.vault_path(run / bench.VAULT_DIRNAME))
        try:
            conn.execute("DELETE FROM source_chunk_embeddings WHERE rowid = (SELECT MIN(rowid) FROM source_chunk_embeddings)")
            conn.commit()
        finally:
            conn.close()
        assert bench.main(["recall", "--run-dir", str(run), "--query", query, *flags]) == bench.EXIT_REFUSED
        assert "does not hold what the manifest says" in capsys.readouterr().err


def test_a_vault_built_without_vectors_is_not_read_as_vectors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Asking a passage vault for ``vectors`` is refused, and the refusal is one line.

    Mutation: let ``vectors`` through for a vault that holds no vector (drop ``require_quality_of_build``).
    """

    run = tmp_path / "bench"
    assert bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--search-quality", "passage"]) == 0
    capsys.readouterr()
    code = bench.main(["recall", "--run-dir", str(run), "--query", "fog horn", "--search-quality", "vectors"])
    err = capsys.readouterr().err
    assert code == bench.EXIT_REFUSED and err.count("\n") == 1
    assert err == (
        "refused: this run recorded no embedding settings, so it was not built with "
        "--search-quality vectors --embeddings on\n"
    )
    for flags in (["--search-quality", "vectors"], ["--embeddings", "on"]):
        refused = bench.main(["build", "--run-dir", str(tmp_path / "again"), "--corpus", str(CORPUS), *flags])
        assert refused == bench.EXIT_REFUSED
        assert "embeddings on" in capsys.readouterr().err
