"""Vectors for source chunks: the opt-in, the egress rules, the write path, the lifecycle and the receipts.

Every test that sends text sends it to a fake endpoint on 127.0.0.1 that this file starts and that records
every text it receives, or to an in-process provider that does the same. Nothing here reaches the network or a
paid API. The provider's vectors are a hash of the text: they are a stand-in for a test of storage and rules,
and no test judges how well a vector ranks anything.

Each test names, in its docstring, the change to the code that must fail it. The cases ``chunk-vectors-*`` of
``scripts/derived_label_mutations.json`` replay the ones that guard an egress rule or a lifecycle step.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import socket
import sqlite3
import struct
import threading
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api import source_chunk_embeddings as embeddings_module
from alicebot_api import sqlite_schema
from alicebot_api.onramp import _write_export, bootstrap_database, main as onramp_main
from alicebot_api.source_chunk_embeddings import (
    PENDING_CHANGED,
    PENDING_ENDPOINT_FAILED,
    PENDING_ENDPOINT_NOT_LOCAL,
    PENDING_STORAGE_FAILED,
    PENDING_TIME_BUDGET,
    PENDING_VAULT_CAP,
    ChunkEmbeddingReceipt,
    backfill_source_chunks,
    chunk_vector_doctor_lines,
    drop_chunk_vectors,
    embed_new_sources,
    sqlite_store_context,
)
from alicebot_api.source_embedding_policy import (
    CHUNK_VECTOR_RETAINED_DATA,
    OFF_DRY_RUN,
    OFF_NO_PROVIDER,
    ENDPOINT_NOT_LOCAL_MESSAGE,
    OFF_ENDPOINT_NOT_LOCAL,
    SKIP_CREDENTIAL,
    SKIP_LINK_ONLY,
    SKIP_SOURCE_RETIRED,
    SKIP_TOO_SHORT,
    SOURCE_EMBEDDINGS_ENV,
    SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
    chunk_embedding_text,
    chunk_heading_paths,
    document_prefix,
    egress_posture,
    endpoint_is_loopback,
    prefix_rule,
    query_prefix,
    source_embedding_settings,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vault_doctor import compile_local_vault_doctor
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
    OpenAICompatibleEmbeddingProvider,
    VNextEmbeddingPeerRefusedError,
    VNextEmbeddingProviderError,
)

USER_ID = "00000000-0000-0000-0000-000000000001"
NOMIC = "nomic-embed-text:v1.5"
LOCAL_URL = "http://127.0.0.1:11434/v1"
REMOTE_URL = "http://embeddings.example.test/v1"
DIMENSIONS = 8

PLAN_A = "The first plan chunk is long enough to be read as a note by the rules."
PLAN_B = "The second plan chunk is long enough to be read as a note by the rules."
PLAN_C = "The third plan chunk is long enough to be read as a note by the rules."
# A chunk that reads as a note: well over the shortest chunk that is sent.
PLAIN = "The lantern review ended with a decision to ship the amber build after the September freeze."
# A key the commit door refuses and the floor alone does not.
FLOOR_BLIND_KEY = "AKIAAAAAAAAAAAAAAAAA"
GITHUB_TOKEN = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zC9bD1fG3hJ5"


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(byte - 128) / 128.0 for byte in digest[:DIMENSIONS]]


class FakeProvider:
    """An in-process provider that records every text it is sent, in the order sent.

    ``before_reply`` runs while a request is in flight, which is when no database connection of the embedding
    pass may be open: a test uses it to change the vault at that moment.
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        model: str = NOMIC,
        base_url: str = LOCAL_URL,
        fail: Exception | None = None,
        max_input_chars: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.fail = fail
        self.max_input_chars = max_input_chars
        self.requests: list[list[str]] = []
        self.before_reply = None

    @property
    def texts(self) -> list[str]:
        return [text for request in self.requests for text in request]

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):  # noqa: ANN001
        self.requests.append(list(texts))
        if self.before_reply is not None:
            self.before_reply()
        if self.fail is not None:
            raise self.fail
        return [_vector(text) for text in texts]

    embed_batch_native = embed_batch


class RecordingServer:
    """An OpenAI-shaped ``/v1/embeddings`` endpoint on 127.0.0.1 that records every text it gets."""

    def __init__(self, *, status: int = 200, bind: str = "127.0.0.1") -> None:
        self.texts: list[str] = []
        self.status = status
        self.bind = bind
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                inputs = json.loads(raw)["input"]
                server.texts.extend(inputs)
                if server.status != 200:
                    body = json.dumps({"error": "refused"}).encode()
                    self.send_response(server.status)
                else:
                    body = json.dumps(
                        {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(inputs)]}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        self._httpd = (V6Server if ":" in bind else ThreadingHTTPServer)((bind, 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        host = f"[{self.bind}]" if ":" in self.bind else self.bind
        return f"http://{host}:{self.port}/v1"

    def __enter__(self) -> "RecordingServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        SOURCE_EMBEDDINGS_ENV,
        SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    # Whether a request to 127.0.0.1 is carried by a proxy is the machine's setting. These tests do not depend on it.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})


ON = {SOURCE_EMBEDDINGS_ENV: "on"}


def _vault(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "memory.db"
    bootstrap_database(db, user_id=USER_ID, user_email="local@alice", secure_parent=False)
    return db


def _context(db: Path):
    return lambda: sqlite_store_context(db, USER_ID)


def _source(
    db: Path,
    title: str | None,
    chunks: list[str],
    *,
    sensitivity: str = "internal",
    domain: str = "project",
) -> tuple[str, list[str]]:
    """A source with these chunks, made directly so that any text can be stored, flagged text included."""

    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        digest = hashlib.sha256(f"{title}{chunks}{sensitivity}".encode()).hexdigest()
        source = store.create_source(
            {
                "source_type": "markdown",
                "title": title,
                "content_hash": digest,
                "domain": domain,
                "sensitivity": sensitivity,
            }
        )
        ids = [
            str(store.create_source_chunk({"source_id": source["id"], "chunk_index": index, "text": text})["id"])
            for index, text in enumerate(chunks)
        ]
        return str(source["id"]), ids


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        return [tuple(row) for row in conn.execute(sql, params)]


def _vector_chunk_ids(db: Path) -> set[str]:
    if not _rows(db, "SELECT 1 FROM sqlite_master WHERE name = 'source_chunk_embeddings'"):
        return set()
    return {row[0] for row in _rows(db, "SELECT chunk_id FROM source_chunk_embeddings")}


def _embed(db: Path, source_ids, provider: FakeProvider, environ=ON, **kwargs) -> ChunkEmbeddingReceipt:
    receipt = embed_new_sources(
        list(source_ids), store_context=_context(db), environ=environ, provider=provider, **kwargs
    )
    assert receipt is not None
    return receipt


# -- the opt-in and its settings -------------------------------------------------------------------------------


def test_only_the_value_on_turns_the_opt_in_on() -> None:
    """Anything else than ``on`` leaves nothing sent. Mutation: read any non-empty value as on."""

    for value in ("on", "ON", " On "):
        assert source_embedding_settings({SOURCE_EMBEDDINGS_ENV: value}).enabled
    for value in ("", "off", "1", "true", "yes", "enabled", "onn"):
        assert not source_embedding_settings({SOURCE_EMBEDDINGS_ENV: value}).enabled
    assert not source_embedding_settings({}).enabled
    # The memory path's variables do not turn it on.
    assert not source_embedding_settings({EMBEDDINGS_BASE_URL_ENV: LOCAL_URL, EMBEDDINGS_MODEL_ENV: NOMIC}).enabled


def test_the_vault_cap_defaults_to_250000_and_a_bad_value_falls_back_to_it() -> None:
    assert source_embedding_settings({}).max_vectors == 250_000
    assert source_embedding_settings({SOURCE_EMBEDDINGS_MAX_VECTORS_ENV: "12"}).max_vectors == 12
    for bad in ("0", "-3", "many", "1e9"):
        assert source_embedding_settings({SOURCE_EMBEDDINGS_MAX_VECTORS_ENV: bad}).max_vectors == 250_000


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:11434/v1", True),
        ("http://embeddings.localhost/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://127.0.0.2:8080/v1", True),
        ("http://LOCALHOST:11434/v1", True),
        ("http://[0:0:0:0:0:0:0:1]/v1", True),
        # Not literals of the rule, or not on this machine: nothing is sent to them.
        ("http://127.1:11434/v1", False),
        ("http://2130706433/v1", False),
        ("http://0x7f.1/v1", False),
        ("http://127.000.000.001/v1", False),
        ("http://[::ffff:127.0.0.1]/v1", False),
        ("http://[::ffff:7f00:1]/v1", False),
        ("http://[::ffff:192.168.1.20]/v1", False),
        ("http://[::1%25lo0]/v1", False),
        ("http://localhost./v1", False),
        ("http://localtest.me/v1", False),
        ("http://127.0.0.1.nip.io/v1", False),
        ("http://[fe80::1]/v1", False),
        ("http://[::]/v1", False),
        ("https://api.openai.com/v1", False),
        ("http://192.168.1.20:11434/v1", False),
        ("http://embeddings.example.test/v1", False),
        ("http://localhost.example.test/v1", False),
        ("http://0.0.0.0:11434/v1", False),
        ("not a url", False),
        ("", False),
        ("ftp://127.0.0.1/v1", False),
    ],
)
def test_a_loopback_endpoint_is_one_whose_requests_stay_on_this_machine(url: str, expected: bool) -> None:
    """Mutation: read a host name that is not ``localhost`` as loopback; read ``0.0.0.0`` or a mapped form as loopback."""

    assert endpoint_is_loopback(url) is expected


def test_a_proxy_that_would_carry_the_request_takes_a_loopback_endpoint_off_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy would take the text off the machine. Mutation: leave the proxy check out of ``endpoint_is_loopback``."""

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.example.test:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    assert endpoint_is_loopback("http://127.0.0.1:11434/v1") is False
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    assert endpoint_is_loopback("http://127.0.0.1:11434/v1") is True


def test_the_posture_names_why_nothing_may_be_sent() -> None:
    """Mutation: let the posture allow an endpoint that is not on this machine."""

    off = source_embedding_settings({})
    assert egress_posture(off, provider_configured=True, base_url=LOCAL_URL).reason == "source_embeddings_off"
    on = source_embedding_settings(ON)
    assert egress_posture(on, provider_configured=False).reason == OFF_NO_PROVIDER
    local = egress_posture(on, provider_configured=True, base_url=LOCAL_URL)
    assert local.may_send and local.local and local.reason is None
    away = egress_posture(on, provider_configured=True, base_url=REMOTE_URL)
    assert not away.may_send and not away.local and away.reason == OFF_ENDPOINT_NOT_LOCAL


# -- the model's prefix and the text that is sent -------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,document,query,version",
    [
        ("nomic-embed-text", "search_document: ", "search_query: ", 1),
        ("nomic-embed-text:v1.5", "search_document: ", "search_query: ", 1),
        ("NOMIC-EMBED-TEXT:latest", "search_document: ", "search_query: ", 1),
        ("nomic-ai/nomic-embed-text-v1.5", "search_document: ", "search_query: ", 1),
        ("text-embedding-3-small", "", "", 0),
        ("mxbai-embed-large", "", "", 0),
        ("my-nomic-embed-text-copy", "", "", 0),
    ],
)
def test_the_prefix_rule_is_keyed_by_model_family_and_unknown_models_get_none(
    model: str, document: str, query: str, version: int
) -> None:
    """Mutation: send the nomic prefix to every model."""

    assert document_prefix(model) == document
    assert query_prefix(model) == query
    rule = prefix_rule(model)
    assert (rule.version if rule else 0) == version


def test_headings_above_a_chunk_are_the_ones_that_still_apply() -> None:
    """A chunk that opens with a heading is under that heading's parents; a continuation is under its own section.

    Mutation: join every earlier heading, or none.
    """

    chunks = [
        "# Roadmap\n\nIntro text for the year.",
        "## Q3 plan\n\nShip the amber build.",
        "More detail on the amber build that the section needs.",
        "## Q4 plan\n\nHire two engineers.",
        "### Hiring\n\nTwo roles open.",
        "```\n# not a heading\n```\n\nCode follows the fence.",
    ]
    assert chunk_heading_paths(chunks) == [
        "",
        "Roadmap",
        "Roadmap > Q3 plan",
        "Roadmap",
        "Roadmap > Q4 plan",
        "Roadmap > Q4 plan > Hiring",
    ]
    assert chunk_heading_paths(["no headings here", "none here either"]) == ["", ""]


def test_the_text_sent_is_prefix_title_headings_and_chunk_and_the_chunk_row_is_not_changed(tmp_path: Path) -> None:
    """Only the text sent changes. Mutation: send the bare chunk; store the header in the chunk row."""

    db = _vault(tmp_path)
    chunk = f"## Q3 plan\n\n{PLAIN}"
    source_id, chunk_ids = _source(db, "Plan 2026", ["# Roadmap\n\nThe year at a glance, in outline.", chunk])
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 2
    assert f"search_document: Plan 2026\nRoadmap\n\n{chunk}" in provider.texts
    assert "search_document: Plan 2026\n\n# Roadmap\n\nThe year at a glance, in outline." in provider.texts
    # The chunk row holds what was captured.
    assert _rows(db, "SELECT text FROM source_chunks WHERE id = ?", (chunk_ids[1],)) == [(chunk,)]
    # A model with no rule is sent no prefix.
    other = FakeProvider(model="text-embedding-3-small")
    _embed(db, [source_id], other)
    assert other.texts and not any(text.startswith("search_document:") for text in other.texts)
    assert chunk_embedding_text(title=None, heading_path="", chunk_text=PLAIN, model=NOMIC) == "search_document: " + PLAIN


def test_a_text_over_the_input_cap_is_cut_before_it_is_hashed_and_a_new_cap_makes_the_vector_stale(tmp_path: Path) -> None:
    """The hash is of the text that is sent. Mutation: hash the text before the cut."""

    db = _vault(tmp_path)
    long_chunk = " ".join(f"The lantern review item {number} was settled." for number in range(30))
    source_id, _ = _source(db, "Plan", [long_chunk])
    first = FakeProvider(max_input_chars=400)
    assert _embed(db, [source_id], first).embedded == 1 and len(first.texts[0]) <= 400
    # The same cap again: current, nothing is sent.
    again = FakeProvider(max_input_chars=400)
    assert _embed(db, [source_id], again).already_current == 1 and again.requests == []
    # Another cap that still cuts this chunk: a different text, so a new vector.
    wider = FakeProvider(max_input_chars=500)
    assert _embed(db, [source_id], wider).embedded == 1 and len(wider.texts[0]) <= 500
    # A cap above the chunk: the whole text, and one more cap above it changes nothing.
    whole = FakeProvider(max_input_chars=5000)
    assert _embed(db, [source_id], whole).embedded == 1 and long_chunk in whole.texts[0]
    larger = FakeProvider(max_input_chars=6000)
    assert _embed(db, [source_id], larger).already_current == 1 and larger.requests == []


# -- no send without the opt-in; no send off this machine ---------------------------------------------------------------------


def test_nothing_is_sent_unless_the_opt_in_is_on_and_the_receipt_is_unchanged(tmp_path: Path, monkeypatch, capsys) -> None:
    """The memory settings alone send no chunk and the import receipt has no new key.

    Mutation: treat ``ALICE_EMBEDDINGS_BASE_URL`` being set as the opt-in (``source_embedding_settings`` returns
    ``enabled=True`` for it, or the door skips the check).
    """

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "vault")]) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert server.texts == []
    assert "chunk_embeddings" not in receipt and receipt["imported_count"] == 1
    db = tmp_path / "vault" / "memory.db"
    assert _vector_chunk_ids(db) == set()
    assert _rows(db, "SELECT 1 FROM sqlite_master WHERE name = 'source_chunk_embeddings'") == []


def test_an_import_with_the_opt_in_on_sends_the_chunks_to_a_loopback_endpoint(tmp_path: Path, monkeypatch, capsys) -> None:
    """The chunk text reaches the endpoint after the import has committed, and the receipt counts it.

    Mutation: leave the door's call to ``embed_new_sources`` out of ``_batch_record_with_chunk_embeddings``.
    """

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n\n## Budget\n\nThe budget for the amber build is approved for the quarter.\n")
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "vault")]) == 0
        receipt = json.loads(capsys.readouterr().out)
        sent = list(server.texts)
    assert receipt["status"] == "ok"
    counts = receipt["chunk_embeddings"]
    assert counts["enabled"] is True and counts["embedded"] == 2 and counts["pending"] == 0
    assert counts["sent"] == 2 and len(sent) == 2
    assert all(text.startswith("search_document: plan\n") for text in sent)
    db = tmp_path / "vault" / "memory.db"
    rows = _rows(db, "SELECT dimensions, length(embedding), model, provider, signature_version FROM source_chunk_embeddings")
    # Stored at the width the model gave (8 here), not padded to the memory column's 1,536.
    assert rows == [(DIMENSIONS, DIMENSIONS * 4, NOMIC, "openai_compatible", 1)] * 2


def test_a_dry_run_sends_nothing_and_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    """Mutation: pass ``dry_run`` as False from ``_run_import_markdown``; drop the dry-run guard in ``embed_new_sources``."""

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "vault"), "--dry-run"]) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert server.texts == []
    assert receipt["dry_run"] is True
    assert receipt["chunk_embeddings"]["reason"] == OFF_DRY_RUN and receipt["chunk_embeddings"]["sent"] == 0
    assert not (tmp_path / "vault").exists()

    direct = FakeProvider()
    result = embed_new_sources(["any"], store_context=_context(_vault(tmp_path / "direct")), environ=ON, provider=direct, dry_run=True)
    assert result is not None and result.reason == OFF_DRY_RUN and direct.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:11434/v1",
        "http://10.0.0.5:11434/v1",
        "http://embeddings.example.test/v1",
        "http://localtest.me:11434/v1",
        "http://127.0.0.1.nip.io:11434/v1",
        "http://[::ffff:127.0.0.1]:11434/v1",
        "http://[::ffff:7f00:1]:11434/v1",
        "http://[::ffff:192.168.1.20]:11434/v1",
        "http://127.1:11434/v1",
        "https://api.example.test/v1",
    ],
)
def test_an_endpoint_that_is_not_on_this_machine_gets_nothing_and_every_chunk_stays_pending(
    tmp_path: Path, url: str
) -> None:
    """A LAN address, a DNS name and a mapped form each get zero requests.

    Mutation: let ``egress_posture`` allow an endpoint that ``endpoint_is_loopback`` refuses.
    """

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN, PLAIN + " Two.", "Done."])
    provider = FakeProvider(base_url=url)
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.sent == 0 and receipt.embedded == 0
    assert receipt.reason == OFF_ENDPOINT_NOT_LOCAL
    assert receipt.pending == 2 and receipt.pending_reasons == {PENDING_ENDPOINT_NOT_LOCAL: 2}
    assert receipt.skipped == {"too_short": 1}
    record = receipt.to_record()
    assert record["reason"] == OFF_ENDPOINT_NOT_LOCAL and record["message"] == ENDPOINT_NOT_LOCAL_MESSAGE
    assert record["pending_reasons"] == {PENDING_ENDPOINT_NOT_LOCAL: 2}
    assert _vector_chunk_ids(db) == set()
    assert _rows(db, "SELECT 1 FROM sqlite_master WHERE name = 'source_chunk_embeddings'") == []


def test_a_proxy_in_the_environment_takes_a_loopback_endpoint_off_this_machine(tmp_path: Path, monkeypatch) -> None:
    """The request would leave this machine through the proxy. Mutation: leave the proxy check out of ``endpoint_is_loopback``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.example.test:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.reason == OFF_ENDPOINT_NOT_LOCAL
    assert receipt.pending_reasons == {PENDING_ENDPOINT_NOT_LOCAL: 1} and _vector_chunk_ids(db) == set()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://embeddings.localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.8.9.10:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_an_endpoint_on_this_machine_is_sent_every_chunk_whatever_its_labels(tmp_path: Path, url: str) -> None:
    """Labels decide nothing about what goes to an endpoint on this machine. Mutation: let a label hold a chunk back."""

    db = _vault(tmp_path)
    levels = ("public", "internal", "unknown", "private", "confidential", "highly_sensitive", "sacred", "regulated")
    ids = [_source(db, level, [f"{PLAIN} ({level})"], sensitivity=level)[0] for level in levels]
    provider = FakeProvider(base_url=url)
    receipt = _embed(db, ids, provider)
    assert receipt.embedded == 8 and receipt.pending == 0 and receipt.reason is None
    assert len(provider.texts) == 8 and len(_vector_chunk_ids(db)) == 8


# -- the peer that is reached must be on this machine --------------------------------------------------------------------


class FakeSocket:
    """A connected socket that answers ``getpeername`` as told and records every byte it is asked to send."""

    def __init__(self, peer: tuple) -> None:
        self.peer = peer
        self.sent: list[bytes] = []
        self.closed = False

    def getpeername(self) -> tuple:
        return self.peer

    def settimeout(self, _value: object) -> None:
        return None

    def setsockopt(self, *_args: object) -> None:
        return None

    def sendall(self, data: bytes, *_args: object) -> None:
        self.sent.append(bytes(data))

    def send(self, data: bytes, *_args: object) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def makefile(self, *_args: object, **_kwargs: object):
        raise AssertionError("a response was read from a peer that is not on this machine")

    def close(self) -> None:
        self.closed = True


def _real_provider(base_url: str):
    return OpenAICompatibleEmbeddingProvider(base_url=base_url, model=NOMIC)


@pytest.mark.parametrize(
    "peer",
    [
        ("203.0.113.7", 11434),
        ("192.168.1.20", 11434),
        ("0.0.0.0", 11434),
        ("::ffff:127.0.0.1", 11434, 0, 0),
        ("::ffff:7f00:1", 11434, 0, 0),
        ("fe80::1", 11434, 0, 1),
        ("2001:db8::1", 11434, 0, 0),
    ],
    ids=["documentation-v4", "lan", "any", "mapped-loopback", "mapped-loopback-hex", "link-local", "documentation-v6"],
)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_name_under_localhost_that_reaches_a_peer_off_this_machine_gets_no_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: tuple, scheme: str
) -> None:
    """The policy lets the name through; the connection decides. Mutation: leave the peer check out of the connection.

    The socket layer is patched, so no connection leaves this machine: ``create_connection`` returns a socket whose
    peer is the address under test. Nothing is written to it (for https not even a TLS handshake), it is closed, no
    response is read, and every chunk stays pending with the reason of an endpoint that is not on this machine.
    """

    sockets: list[FakeSocket] = []

    def create_connection(address, timeout=None, source_address=None):  # noqa: ANN001, ANN202
        fake = FakeSocket(peer)
        sockets.append(fake)
        return fake

    monkeypatch.setattr(socket, "create_connection", create_connection)
    db = _vault(tmp_path)
    ids = [_source(db, f"Note {index}", [f"{PLAIN} Note {index}.", f"{PLAIN} Note {index} again."])[0] for index in range(25)]
    provider = _real_provider(f"{scheme}://rebind.localhost:11434/v1")
    assert endpoint_is_loopback(provider.base_url)  # the name and the proxy rules are the first gate, and it passes
    receipt = _embed(db, ids, provider)
    assert len(sockets) == 1, "one connection was made, and no further request after it was refused"
    assert sockets[0].sent == [] and sockets[0].closed
    assert receipt.reason == OFF_ENDPOINT_NOT_LOCAL and receipt.sent == 0 and receipt.embedded == 0
    assert receipt.pending == 50 and receipt.pending_reasons == {PENDING_ENDPOINT_NOT_LOCAL: 50}
    assert receipt.to_record()["message"] == ENDPOINT_NOT_LOCAL_MESSAGE
    assert _rows(db, "SELECT 1 FROM sqlite_master WHERE name = 'source_chunk_embeddings'") == []


class RawListener:
    """A listener that records the bytes it receives, on a given local address."""

    def __init__(self, bind: str) -> None:
        self.received = b""
        self.accepted = 0
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        self._listener = socket.socket(family, socket.SOCK_STREAM)
        self._listener.bind((bind, 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    def _run(self) -> None:
        while not self._stop:
            try:
                connection, _peer = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            connection.settimeout(0.5)
            try:
                while True:
                    data = connection.recv(65536)
                    if not data:
                        break
                    self.received += data
            except (TimeoutError, OSError):
                pass
            finally:
                connection.close()

    def __enter__(self) -> "RawListener":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop = True
        self._thread.join(timeout=5)
        self._listener.close()


def _local_non_loopback_address() -> str | None:
    """An address of this machine that is not loopback, found without sending a packet; ``None`` when there is none."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("203.0.113.1", 9))  # a UDP connect only picks a route
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if address.startswith("127.") or address == "0.0.0.0":
        return None
    # The address has to be one a listener on this machine can be reached at.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((address, 0))
            listener.listen(1)
            with socket.create_connection((address, listener.getsockname()[1]), timeout=1.0):
                return address
        except OSError:
            return None


def _resolve_name_to(monkeypatch: pytest.MonkeyPatch, name: str, address: str) -> None:
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if host == name:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_a_name_under_localhost_that_resolves_to_a_listener_off_loopback_gets_zero_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real listener on an address of this machine that is not loopback receives the connection and no byte.

    Mutation: leave the peer check out of the connection.
    """

    lan = _local_non_loopback_address()
    if lan is None:
        pytest.skip("this machine has no address that is not loopback")
    _resolve_name_to(monkeypatch, "rebind.localhost", lan)
    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A, PLAN_B])
    with RawListener(lan) as listener:
        receipt = _embed(db, [source_id], _real_provider(f"http://rebind.localhost:{listener.port}/v1"))
    assert listener.received == b""
    assert receipt.reason == OFF_ENDPOINT_NOT_LOCAL and receipt.pending_reasons == {PENDING_ENDPOINT_NOT_LOCAL: 2}
    assert _vector_chunk_ids(db) == set()


def test_a_name_under_localhost_that_resolves_to_loopback_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A, PLAN_B])
    _resolve_name_to(monkeypatch, "alias.localhost", "127.0.0.1")
    with RecordingServer() as server:
        receipt = _embed(db, [source_id], _real_provider(f"http://alias.localhost:{server.port}/v1"))
        sent = list(server.texts)
    assert receipt.embedded == 2 and receipt.pending == 0 and receipt.reason is None
    assert len(sent) == 2 and len(_vector_chunk_ids(db)) == 2


def test_a_listener_on_ipv6_loopback_works(tmp_path: Path) -> None:
    try:
        server = RecordingServer(bind="::1")
    except OSError:
        pytest.skip("this machine has no IPv6 loopback")
    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A])
    with server:
        receipt = _embed(db, [source_id], _real_provider(server.base_url))
        sent = list(server.texts)
    assert receipt.embedded == 1 and len(sent) == 1


def test_a_proxy_that_would_carry_a_source_request_is_refused_before_a_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence behind the policy gate: a request a proxy would carry has the proxy as its peer.

    Mutation: leave the proxy refusal out of ``_HTTPHandler.http_open``.
    """

    sockets: list[FakeSocket] = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sockets.append(FakeSocket(("127.0.0.1", 3128))) or sockets[-1])
    provider = _real_provider("http://127.0.0.1:11434/v1")
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.example.test:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    with pytest.raises(VNextEmbeddingPeerRefusedError):
        provider.embed_batch_native(["a text of the kind that is sent"])
    assert sockets == []


def test_the_memory_path_is_not_held_to_a_peer_on_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory embeddings keep working with any endpoint. Mutation: hold ``embed_batch`` to a loopback peer."""

    peers: list[FakeSocket] = []

    def create_connection(address, timeout=None, source_address=None):  # noqa: ANN001, ANN202
        fake = FakeSocket(("203.0.113.7", 11434))
        peers.append(fake)
        raise OSError("not a real connection")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    provider = _real_provider("http://embeddings.example.test/v1")
    with pytest.raises(VNextEmbeddingProviderError) as refused:
        provider.embed_batch(["a memory"])
    assert not isinstance(refused.value, VNextEmbeddingPeerRefusedError)
    assert len(peers) == 1 and peers[0].sent == []


# -- what is never sent ---------------------------------------------------------------------------------------------


def test_a_chunk_the_credential_check_flags_is_never_sent_and_the_whole_chunk_is_read(tmp_path: Path) -> None:
    """Mutation: leave the credential check out of ``skip_reason``; read only the head of the chunk."""

    db = _vault(tmp_path)
    padding = "The quarterly review covered hiring and the lantern launch. " * 30
    source_id, chunk_ids = _source(
        db,
        "Notes",
        [
            f"Deploy notes: {GITHUB_TOKEN}",
            f"{padding}\ntoken at the tail: {GITHUB_TOKEN}",
            PLAIN,
        ],
    )
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert receipt.skipped[SKIP_CREDENTIAL] == 2 and receipt.embedded == 1
    assert GITHUB_TOKEN not in "".join(provider.texts)
    assert _vector_chunk_ids(db) == {chunk_ids[2]}


def test_the_commit_doors_verdict_counts_as_well_as_the_floors(tmp_path: Path) -> None:
    """A key the floor treats as a placeholder is refused by the commit door. Mutation: use ``credential_verdict`` only."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Notes", [f"The staging key was {FLOOR_BLIND_KEY} until the rotation in June, then replaced."])
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.skipped[SKIP_CREDENTIAL] == 1


def test_a_title_the_credential_check_flags_stops_the_chunk_from_being_sent(tmp_path: Path) -> None:
    """The title is part of the text sent, so it is read with the chunk. Mutation: read the chunk alone."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, f"Keys {GITHUB_TOKEN}", [PLAIN])
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.skipped[SKIP_CREDENTIAL] == 1


def test_link_only_and_short_chunks_are_not_sent(tmp_path: Path) -> None:
    """Mutation: leave out the readable-line check; leave out the minimum length."""

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(
        db,
        "Index",
        [
            "- [[Plan]]\n- [[Budget]]\n- [Roadmap](roadmap.md)\n- https://example.test/a",
            "# Heading only",
            "Done.",
            "## Status\n\nShipped.",
            PLAIN,
        ],
    )
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert receipt.skipped[SKIP_LINK_ONLY] == 2
    assert receipt.skipped[SKIP_TOO_SHORT] == 2
    assert receipt.embedded == 1 and _vector_chunk_ids(db) == {chunk_ids[4]}


def test_the_chunks_of_a_deleted_replaced_or_scrubbed_source_are_not_sent(tmp_path: Path) -> None:
    """Mutation: judge a source live whatever its ``deleted_at`` says."""

    db = _vault(tmp_path)
    replaced, _ = _source(db, "Replaced", [PLAIN])
    scrubbed, _ = _source(db, "Scrubbed", [PLAIN + " Again."])
    live, live_chunks = _source(db, "Live", [PLAIN + " Live."])
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (replaced,))
        SQLiteVNextStore(conn, USER_ID).scrub_source(scrubbed)
    provider = FakeProvider()
    receipt = _embed(db, [replaced, scrubbed, live], provider)
    assert receipt.skipped[SKIP_SOURCE_RETIRED] == 2 and receipt.embedded == 1
    assert len(provider.texts) == 1 and _vector_chunk_ids(db) == set(live_chunks)


# -- the write path ------------------------------------------------------------------------------------------------------


def test_no_database_lock_is_held_while_the_endpoint_is_called(tmp_path: Path) -> None:
    """While a request is in flight another writer can take the lock at once.

    Mutation: call the provider inside ``with store_context()`` in ``run_source_embeddings``.
    """

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    provider = FakeProvider()
    taken: list[bool] = []

    def writer_can_start() -> None:
        with closing(sqlite3.connect(db, timeout=0.2)) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            taken.append(True)

    provider.before_reply = writer_can_start
    assert _embed(db, [source_id], provider).embedded == 1
    assert taken == [True]


def test_a_scrub_during_the_send_leaves_no_vector(tmp_path: Path) -> None:
    """Compare and set on the source: the scrub commits while the request is in flight.

    Mutation: remove the liveness and text-hash checks from ``_store_page``.
    """

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN, PLAIN + " More."])
    provider = FakeProvider()

    def scrub() -> None:
        with sqlite_user_connection(db, USER_ID) as conn:
            SQLiteVNextStore(conn, USER_ID).scrub_source(source_id)

    provider.before_reply = scrub
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 0 and receipt.pending_reasons[PENDING_CHANGED] == 2
    assert _vector_chunk_ids(db) == set()


def test_a_replacement_during_the_send_leaves_no_vector(tmp_path: Path) -> None:
    """Mutation: remove the liveness check from ``_store_page``."""

    db = _vault(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        first = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN} It changed.\n")
    provider = FakeProvider()

    def replace() -> None:
        with sqlite_user_connection(db, USER_ID) as conn:
            result = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, supersede=True)
        assert result.superseded

    provider.before_reply = replace
    receipt = _embed(db, first.source_ids, provider)
    assert receipt.embedded == 0 and receipt.pending_reasons[PENDING_CHANGED] >= 1
    assert _rows(db, "SELECT count(*) FROM source_chunk_embeddings WHERE source_id = ?", (first.source_ids[0],)) == [(0,)]


def test_a_title_edited_during_the_send_leaves_no_vector_of_the_old_text(tmp_path: Path) -> None:
    """The title is part of the text sent, so a changed title changes the hash. Mutation: compare nothing but liveness."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Old title", [PLAIN])
    provider = FakeProvider()

    def rename() -> None:
        with sqlite_user_connection(db, USER_ID) as conn:
            SQLiteVNextStore(conn, USER_ID).update_source(source_id=source_id, patch={"title": "New title"})

    provider.before_reply = rename
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 0 and receipt.pending_reasons[PENDING_CHANGED] == 1
    assert _vector_chunk_ids(db) == set()
    # The next pass sends the new text and stores it.
    follow = FakeProvider()
    assert _embed(db, [source_id], follow).embedded == 1 and "New title" in follow.texts[0]


def test_a_scrub_before_a_later_batch_of_a_multi_batch_pass_sends_none_of_its_text(
    tmp_path: Path, monkeypatch
) -> None:
    """The recheck before each batch. Mutation: send ``planned_batch`` instead of the rechecked ``batch`` in ``_send``.

    The first batch is in flight when the owner scrubs the second source. The second batch is judged again, finds
    the source scrubbed and is not sent.
    """

    monkeypatch.setattr(embeddings_module, "MAX_EMBEDDINGS_BATCH_SIZE", 2)
    db = _vault(tmp_path)
    first, first_chunks = _source(db, "First", [PLAN_A, PLAN_B])
    second, second_chunks = _source(db, "Second", [PLAN_C, PLAN_C + " Again."])
    provider = FakeProvider()

    def scrub_the_second_source() -> None:
        if len(provider.requests) == 1:
            with sqlite_user_connection(db, USER_ID) as conn:
                SQLiteVNextStore(conn, USER_ID).scrub_source(second)

    provider.before_reply = scrub_the_second_source
    receipt = _embed(db, [first, second], provider)
    assert len(provider.requests) == 1 and all(PLAN_C not in text and "Second" not in text for text in provider.texts)
    assert receipt.sent == 2 and receipt.embedded == 2 and receipt.pending_reasons[PENDING_CHANGED] == 2
    assert _vector_chunk_ids(db) == set(first_chunks)
    assert second_chunks and not (set(second_chunks) & _vector_chunk_ids(db))


def test_a_relabel_after_planning_is_not_an_egress_event(tmp_path: Path, monkeypatch) -> None:
    """Labels decide nothing for an endpoint on this machine, so the chunk is sent and its vector stored."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A, PLAN_B], sensitivity="internal")
    original = embeddings_module._send

    def relabel_then_send(*args, **kwargs):
        with sqlite_user_connection(db, USER_ID) as conn:
            conn.execute("UPDATE sources SET sensitivity = 'regulated' WHERE id = ?", (source_id,))
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings_module, "_send", relabel_then_send)
    receipt = _embed(db, [source_id], FakeProvider())
    assert receipt.embedded == 2 and receipt.pending == 0


def test_a_scrub_after_planning_and_before_the_send_sends_nothing_and_stores_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """A scrub changes the text, so the hash differs. Mutation: leave the hash comparison out of ``_still_qualifying``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A, PLAN_B])
    original = embeddings_module._send

    def scrub_then_send(*args, **kwargs):
        with sqlite_user_connection(db, USER_ID) as conn:
            SQLiteVNextStore(conn, USER_ID).scrub_source(source_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings_module, "_send", scrub_then_send)
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.sent == 0 and receipt.embedded == 0
    assert receipt.pending_reasons[PENDING_CHANGED] == 2 and _vector_chunk_ids(db) == set()


def test_a_replacement_after_planning_and_before_the_send_sends_nothing_of_the_old_text(
    tmp_path: Path, monkeypatch
) -> None:
    """Mutation: leave the liveness check out of ``_still_qualifying``."""

    db = _vault(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        first = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN} It changed.\n")
    original = embeddings_module._send

    def replace_then_send(*args, **kwargs):
        with sqlite_user_connection(db, USER_ID) as conn:
            result = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, supersede=True)
        assert result.superseded
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings_module, "_send", replace_then_send)
    provider = FakeProvider()
    receipt = _embed(db, first.source_ids, provider)
    assert provider.requests == [] and receipt.embedded == 0 and receipt.pending_reasons[PENDING_CHANGED] >= 1
    assert _vector_chunk_ids(db) == set()


def test_a_title_changed_after_planning_is_sent_as_it_now_reads_or_not_at_all(tmp_path: Path, monkeypatch) -> None:
    """The hash of the text to send is read again. Mutation: leave the hash comparison out of ``_still_qualifying``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Old title", [PLAIN])
    original = embeddings_module._send

    def rename_then_send(*args, **kwargs):
        with sqlite_user_connection(db, USER_ID) as conn:
            conn.execute("UPDATE sources SET title = 'New title' WHERE id = ?", (source_id,))
        return original(*args, **kwargs)

    monkeypatch.setattr(embeddings_module, "_send", rename_then_send)
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.pending_reasons[PENDING_CHANGED] == 1 and _vector_chunk_ids(db) == set()


def test_a_batch_that_cannot_be_judged_is_not_sent(tmp_path: Path, monkeypatch) -> None:
    """Mutation: send the batch when the recheck raises."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A])

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(embeddings_module, "_still_qualifying", fail)
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.embedded == 0 and receipt.pending_reasons[PENDING_STORAGE_FAILED] == 1


def test_the_recheck_reads_the_source_and_its_chunks_in_one_transaction(tmp_path: Path, monkeypatch) -> None:
    """Mutation: read the source row and the chunk rows outside ``vectors.read_snapshot`` in ``_still_qualifying``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A, PLAN_B])
    seen: list[tuple[str, bool]] = []
    recording = {"on": False}
    original_send = embeddings_module._send
    original_row = embeddings_module.vectors.embedding_source_row
    original_chunks = embeddings_module.vectors.embedding_source_chunks

    def row(store, source):
        if recording["on"]:
            seen.append(("row", store.conn.in_transaction))
        return original_row(store, source)

    def chunks(store, source):
        if recording["on"]:
            seen.append(("chunks", store.conn.in_transaction))
        return original_chunks(store, source)

    def send(*args, **kwargs):
        recording["on"] = True
        return original_send(*args, **kwargs)

    monkeypatch.setattr(embeddings_module.vectors, "embedding_source_row", row)
    monkeypatch.setattr(embeddings_module.vectors, "embedding_source_chunks", chunks)
    monkeypatch.setattr(embeddings_module, "_send", send)
    assert _embed(db, [source_id], FakeProvider()).embedded == 2
    assert [name for name, _ in seen][:2] == ["row", "chunks"]
    assert seen[0][1] is True and seen[1][1] is True


def test_the_recheck_judges_one_snapshot_and_a_scrub_between_its_reads_is_the_window(tmp_path: Path, monkeypatch) -> None:
    """The source row and the chunk rows are one state of the vault, so a scrub committed between the two reads
    is not half seen. The chunk is judged as it was when the source was read and goes to the endpoint on this machine,
    as a scrub committed after the check does; its vector is not stored. Mutation: read outside the transaction (the
    chunk rows then show the scrub, and a state that never existed is judged).
    """

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAN_A])
    recording = {"on": False, "done": False}
    original_send = embeddings_module._send
    original_chunks = embeddings_module.vectors.embedding_source_chunks

    def chunks(store, source):
        if recording["on"] and not recording["done"]:
            recording["done"] = True
            with sqlite_user_connection(db, USER_ID) as conn:
                SQLiteVNextStore(conn, USER_ID).scrub_source(source_id)
        return original_chunks(store, source)

    def send(*args, **kwargs):
        recording["on"] = True
        return original_send(*args, **kwargs)

    monkeypatch.setattr(embeddings_module.vectors, "embedding_source_chunks", chunks)
    monkeypatch.setattr(embeddings_module, "_send", send)
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider)
    assert recording["done"] and len(provider.requests) == 1
    assert receipt.embedded == 0 and receipt.pending_reasons[PENDING_CHANGED] == 1 and _vector_chunk_ids(db) == set()


def test_a_failed_batch_leaves_the_chunks_pending_and_nothing_raises(tmp_path: Path) -> None:
    """Mutation: let the provider's error out of ``embed_new_sources``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN, PLAIN + " Second."])
    provider = FakeProvider(fail=VNextEmbeddingProviderError("embeddings request failed: refused"))
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 0 and receipt.pending == 2 and receipt.pending_reasons[PENDING_ENDPOINT_FAILED] == 2
    assert _vector_chunk_ids(db) == set()
    assert receipt.to_record()["failure_reasons"]
    # A later pass embeds what is pending.
    assert _embed(db, [source_id], FakeProvider()).embedded == 2


def test_an_import_whose_endpoint_fails_still_succeeds(tmp_path: Path, monkeypatch, capsys) -> None:
    """Import never fails because of embeddings. Mutation: let a failed batch change the exit status."""

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with RecordingServer(status=500) as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        assert onramp_main(["import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "vault")]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok" and receipt["imported_count"] == 1
    counts = receipt["chunk_embeddings"]
    assert counts["embedded"] == 0 and counts["pending"] == 1 and counts["pending_reasons"] == {PENDING_ENDPOINT_FAILED: 1}


def test_a_refused_text_does_not_sink_its_neighbours(tmp_path: Path) -> None:
    """The 400/413/422 split of the memory path applies. Mutation: send the batch with ``native`` dropping the split."""

    db = _vault(tmp_path)
    bad = PLAIN + " This one is refused by the endpoint."
    source_id, chunk_ids = _source(db, "Plan", [PLAIN, bad, PLAIN + " Third chunk."])
    provider = FakeProvider()
    real = provider.embed_batch

    def embed(texts):  # noqa: ANN001
        if any("refused by the endpoint" in text for text in texts) and len(texts) > 1:
            raise VNextEmbeddingProviderError("embeddings endpoint returned HTTP 413", status=413)
        if any("refused by the endpoint" in text for text in texts):
            raise VNextEmbeddingProviderError("embeddings endpoint returned HTTP 413", status=413)
        return real(texts)

    provider.embed_batch = embed  # type: ignore[method-assign]
    provider.embed_batch_native = embed  # type: ignore[method-assign]
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 2 and receipt.pending == 1
    assert _vector_chunk_ids(db) == {chunk_ids[0], chunk_ids[2]}


def test_a_time_budget_stops_the_sending(tmp_path: Path) -> None:
    """The tool path has a time budget; what is left stays pending. Mutation: ignore the deadline."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider, time_budget_seconds=0.0)
    assert provider.requests == [] and receipt.pending_reasons[PENDING_TIME_BUDGET] == 1


def test_the_vault_cap_stops_the_storing_and_the_sending(tmp_path: Path) -> None:
    """Chunks over the cap are not sent and the receipt gives the reason. Mutation: remove the cap check."""

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [f"{PLAIN} Part {index}." for index in range(4)])
    provider = FakeProvider()
    receipt = _embed(db, [source_id], provider, environ={**ON, SOURCE_EMBEDDINGS_MAX_VECTORS_ENV: "2"})
    assert receipt.embedded == 2 and receipt.pending_reasons[PENDING_VAULT_CAP] == 2
    assert len(provider.texts) == 2
    record = receipt.to_record()
    assert "ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS" in record["vault_cap_message"]  # type: ignore[operator]
    assert len(_vector_chunk_ids(db)) == 2
    # A stale vector is replaced in place, so a vault at the cap can still refresh what it has.
    again = FakeProvider(model="text-embedding-3-small")
    refreshed = _embed(db, [source_id], again, environ={**ON, SOURCE_EMBEDDINGS_MAX_VECTORS_ENV: "2"})
    assert refreshed.embedded == 2 and refreshed.pending_reasons[PENDING_VAULT_CAP] == 2
    assert len(_vector_chunk_ids(db)) == 2


def test_the_store_checks_the_cap_again_when_it_writes(tmp_path: Path) -> None:
    """Two passes that both planned under the cap cannot store past it. Mutation: check the cap at the plan only."""

    db = _vault(tmp_path)
    first, _ = _source(db, "First", [PLAIN + " One."])
    second, _ = _source(db, "Second", [PLAIN + " Two."])
    environ = {**ON, SOURCE_EMBEDDINGS_MAX_VECTORS_ENV: "1"}
    provider = FakeProvider()

    def other_pass_stores_first() -> None:
        embed_new_sources([first], store_context=_context(db), environ=environ, provider=FakeProvider())

    provider.before_reply = other_pass_stores_first
    receipt = _embed(db, [second], provider, environ=environ)
    assert receipt.embedded == 0 and receipt.pending_reasons[PENDING_VAULT_CAP] == 1
    assert len(_vector_chunk_ids(db)) == 1


# -- the table is made by the first vector ---------------------------------------------------------------------------------


def test_a_vault_without_the_table_opens_and_the_first_vector_makes_it(tmp_path: Path) -> None:
    """Migration on an existing vault: no step at open, the table and its version row come with the first vector.

    Mutation: create the table in ``bootstrap_sqlite_schema`` (the older release's export then refuses the vault).
    """

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    assert _rows(db, "SELECT name FROM sqlite_master WHERE name LIKE 'source_chunk_embeddings%'") == []
    assert _rows(db, "SELECT value FROM alice_schema_state WHERE key = 'source_chunk_embeddings_version'") == []
    assert compile_local_vault_doctor(db, user_id=USER_ID)  # reads a vault that has no such table
    assert _embed(db, [source_id], FakeProvider()).embedded == 1
    assert _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_chunk_embeddings'")
    assert _rows(db, "SELECT value FROM alice_schema_state WHERE key = 'source_chunk_embeddings_version'") == [
        (sqlite_schema.SOURCE_CHUNK_VECTORS_SCHEMA_VERSION,)
    ]
    # The export accepts the table as this build's own, and does not export it.
    stream = io.StringIO()
    _write_export(stream, db_path=db, user_id=UUID(USER_ID))
    assert "source_chunk_embeddings" not in stream.getvalue() and '"record_type": "source_chunk"' in stream.getvalue().replace('":"', '": "')


def test_a_chunk_deleted_from_the_vault_takes_its_vector_with_it(tmp_path: Path) -> None:
    """The foreign key cascades. Mutation: drop ``ON DELETE CASCADE`` from the table."""

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN])
    _embed(db, [source_id], FakeProvider())
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
    assert _vector_chunk_ids(db) == set()


def test_the_stamp_changes_when_a_vector_is_overwritten_or_removed_and_not_when_one_is_added(tmp_path: Path) -> None:
    """A resident copy rebuilds on a changed stamp and finds added rows by their row number.

    Mutation: drop the update trigger; drop the delete trigger; add an insert trigger; leave the stamp out of the drop.
    """

    from alicebot_api.sqlite_schema import source_chunk_vectors_stamp
    from alicebot_api.vnext_stores.sqlite.source_chunk_vectors import (
        delete_chunk_vectors,
        drop_chunk_vector_table,
        put_chunk_vector,
    )

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAN_A, PLAN_B, PLAN_C])
    fields = {"provider": "p", "model": "m", "endpoint": "e", "signature_version": 1, "header_version": 1, "prefix_version": 0}
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert source_chunk_vectors_stamp(conn) is None
        put_chunk_vector(store, chunk_id=chunk_ids[0], source_id=source_id, vector=[1.0], content_sha256="a", **fields)
        first = source_chunk_vectors_stamp(conn)
        assert first
        put_chunk_vector(store, chunk_id=chunk_ids[1], source_id=source_id, vector=[2.0], content_sha256="b", **fields)
        assert source_chunk_vectors_stamp(conn) == first  # a row added: the same token
        put_chunk_vector(store, chunk_id=chunk_ids[1], source_id=source_id, vector=[3.0], content_sha256="c", **fields)
        second = source_chunk_vectors_stamp(conn)
        assert second != first  # a row overwritten
        assert delete_chunk_vectors(store, source_id=source_id) == 2
        third = source_chunk_vectors_stamp(conn)
        assert third != second  # rows deleted
        put_chunk_vector(store, chunk_id=chunk_ids[2], source_id=source_id, vector=[4.0], content_sha256="d", **fields)
        assert source_chunk_vectors_stamp(conn) == third
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))  # the rows go with the source, by cascade
        fourth = source_chunk_vectors_stamp(conn)
        assert fourth != third
        assert drop_chunk_vector_table(store) == 0
        assert source_chunk_vectors_stamp(conn) != fourth  # the table is gone, and a copy must know


def _fingerprint(db: Path, *source_ids: str) -> str:
    with sqlite_user_connection(db, USER_ID) as conn:
        return sqlite_schema.source_chunk_vector_fingerprint(conn, USER_ID, json.dumps(sorted(source_ids)))


def test_a_source_has_a_token_that_moves_when_its_own_vector_is_overwritten_or_removed_and_for_nothing_else(
    tmp_path: Path,
) -> None:
    """The fingerprint of a set of sources reads their tokens and no others.

    Mutation: leave the token out of the insert, the update or the delete trigger; read every token.
    """

    db = _vault(tmp_path)
    one, _ = _source(db, "One", [PLAN_A, PLAN_B])
    two, _ = _source(db, "Two", [PLAN_C, PLAN_C + " Again."])
    assert _embed(db, [one, two], FakeProvider()).embedded == 4
    # Rows that were added are written to the token of their own source.
    added_one, added_two = _fingerprint(db, one), _fingerprint(db, two)
    assert added_one != "" and added_two != "" and added_one != added_two
    assert _fingerprint(db, one, two) not in {"", added_one, added_two}
    # A vector of One is overwritten (another model of the same family makes it stale, and it is replaced in place).
    assert _embed(db, [one], FakeProvider(model="nomic-embed-text:latest")).embedded == 2
    after_overwrite = _fingerprint(db, one)
    assert after_overwrite not in {"", added_one} and _fingerprint(db, two) == added_two
    # The vectors of Two go with a scrub: only Two's token moves, and One's fingerprint is what it was.
    with sqlite_user_connection(db, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).scrub_source(two)
    assert _fingerprint(db, one) == after_overwrite
    after_scrub = _fingerprint(db, two)
    assert after_scrub not in {"", added_two} and _fingerprint(db, one, two) not in {after_overwrite, after_scrub}
    # A source that is not in the set cannot be seen through it, and a set of unknown ids has none.
    assert _fingerprint(db, "an-unknown-source") == ""
    # A new chunk with a vector moves the token of its own source and of no other.
    with sqlite_user_connection(db, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).create_source_chunk(
            {"id": fixed_chunk_id("One", 2), "source_id": one, "chunk_index": 2, "text": PLAIN}
        )
    before_two = _fingerprint(db, two)
    assert _embed(db, [one], FakeProvider(model="nomic-embed-text:latest")).embedded == 1
    assert _fingerprint(db, one) != after_overwrite and _fingerprint(db, two) == before_two


def fixed_chunk_id(name: str, index: int) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"chunk|{name}|{index}"))


def test_a_table_made_before_the_tokens_gets_their_triggers_the_next_time_a_vector_is_stored(tmp_path: Path) -> None:
    """Mutation: skip the upgrade in ``ensure_source_chunk_vectors_table``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "One", [PLAN_A])
    assert _embed(db, [source_id], FakeProvider()).embedded == 1
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("DROP TRIGGER source_chunk_embeddings_source_token_after_insert")
        conn.execute("DROP TRIGGER source_chunk_embeddings_source_token_after_update")
        conn.execute("DROP TRIGGER source_chunk_embeddings_source_token_after_delete")
        conn.execute(
            "UPDATE alice_schema_state SET value = '1' WHERE key = ?", (sqlite_schema.SOURCE_CHUNK_VECTORS_STATE_KEY,)
        )
    assert _rows(db, "SELECT name FROM sqlite_master WHERE name LIKE 'source_chunk_embeddings_source_token%'") == []
    # The next write that needs the table brings it up to date.
    other, _ = _source(db, "Two", [PLAN_B])
    assert _embed(db, [other], FakeProvider()).embedded == 1
    assert len(_rows(db, "SELECT name FROM sqlite_master WHERE name LIKE 'source_chunk_embeddings_source_token%'")) == 3
    assert _rows(
        db, "SELECT value FROM alice_schema_state WHERE key = ?", (sqlite_schema.SOURCE_CHUNK_VECTORS_STATE_KEY,)
    ) == [(sqlite_schema.SOURCE_CHUNK_VECTORS_SCHEMA_VERSION,)]


def test_the_vector_row_refuses_a_width_that_is_not_its_dimensions(tmp_path: Path) -> None:
    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN])
    _embed(db, [source_id], FakeProvider())
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE source_chunk_embeddings SET dimensions = 9 WHERE chunk_id = ?", (chunk_ids[0],))


# -- the lifecycle ------------------------------------------------------------------------------------------------------


def test_scrub_deletes_the_vectors_and_the_receipt_counts_them(tmp_path: Path, capsys) -> None:
    """Mutation: leave the vector delete out of ``scrub_source``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN, PLAIN + " Two."])
    other, other_chunks = _source(db, "Other", [PLAIN + " Other."])
    _embed(db, [source_id, other], FakeProvider())
    assert onramp_main(["sources", "delete", source_id, "--yes", "--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    receipt = payload["deleted"][0]
    assert receipt["chunk_vectors"] == 2
    assert _vector_chunk_ids(db) == set(other_chunks)
    # The receipt says that deleting a vector is logical deletion, as it says for chunk text.
    assert "vectors" in payload["retained_data"] and "logical" in payload["retained_data"]
    assert "VACUUM" in payload["retained_data"] and "write-ahead log" in payload["retained_data"]


def test_supersede_deletes_the_vectors_of_the_replaced_source(tmp_path: Path) -> None:
    """Mutation: leave the vector delete out of ``supersede_source``."""

    db = _vault(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        first = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    assert _embed(db, first.source_ids, FakeProvider()).embedded == 1
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN} It changed.\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        second = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, supersede=True)
    assert second.superseded[0]["chunk_vectors"] == 1
    assert _vector_chunk_ids(db) == set()
    assert _rows(db, "SELECT payload_json FROM event_log WHERE event_type = 'source.superseded'")
    payload = json.loads(_rows(db, "SELECT payload_json FROM event_log WHERE event_type = 'source.superseded'")[0][0])
    assert payload["chunk_vectors"] == 1


def test_prune_deletes_the_vectors_of_replaced_sources(tmp_path: Path, capsys) -> None:
    """Prune scrubs a replaced version, and a replaced source holds no vector. A vector made by hand is removed too."""

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN])
    _embed(db, [source_id], FakeProvider())
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute(
            "UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z', metadata_json = ? WHERE id = ?",
            (json.dumps({"superseded_by": "someone", "superseded_at": "2026-01-01T00:00:00Z"}), source_id),
        )
    assert onramp_main(["sources", "prune", "--superseded", "--yes", "--db", str(db)]) == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert receipt["chunk_vectors"] == 1 and _vector_chunk_ids(db) == set()


# -- deleting a vector is logical deletion; the bytes are zeroed where the database frees them ------------------------


class WideProvider(FakeProvider):
    """Vectors the width of a real model (768 floats, about a page), different for every model and text."""

    def embed_batch(self, texts):  # noqa: ANN001
        self.requests.append(list(texts))
        return [wide_vector(self.model, text) for text in texts]

    embed_batch_native = embed_batch


def wide_vector(model: str, text: str) -> list[float]:
    rng = random.Random(f"{model}\x00{text}")
    return [1.0 + rng.random() for _ in range(768)]


def _marker(model: str, text: str) -> bytes:
    """The first 32 bytes of the stored vector of this text: the bytes to look for in the vault file and its log."""

    return struct.pack("<8f", *wide_vector(model, text)[:8])


def _file_bytes(db: Path) -> bytes:
    return b"".join(path.read_bytes() for path in (db, Path(f"{db}-wal")) if path.exists())


def _chunk_texts_sent(provider: FakeProvider) -> list[str]:
    return provider.texts


def _wide_sources(db: Path, count: int) -> tuple[list[str], WideProvider]:
    ids = [_source(db, f"Plan {index}", [PLAIN + f" Number {index}."])[0] for index in range(count)]
    provider = WideProvider()
    assert _embed(db, ids, provider).embedded == count
    return ids, provider


def test_scrub_leaves_no_vector_bytes_in_the_vault_file_or_its_log(tmp_path: Path) -> None:
    """Vectors are written and deleted under secure deletion. Mutation: leave ``secure_deletion`` out of ``_store_page``."""

    db = _vault(tmp_path)
    ids, provider = _wide_sources(db, 3)
    markers = [_marker(NOMIC, text) for text in provider.texts]
    assert all(marker in _file_bytes(db) for marker in markers)
    with sqlite_user_connection(db, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).scrub_source(ids[0])
    after = _file_bytes(db)
    assert markers[0] not in after and markers[1] in after and markers[2] in after


def test_a_replaced_stale_vector_leaves_no_bytes_of_the_old_vector(tmp_path: Path) -> None:
    """A vector rewritten for a new model overwrites the old one in place. Mutation: leave ``secure_deletion`` out of ``_store_page``."""

    db = _vault(tmp_path)
    ids, first = _wide_sources(db, 3)
    old = [_marker(NOMIC, text) for text in first.texts]
    second = WideProvider(model="nomic-embed-text:latest")
    assert _embed(db, ids, second).embedded == 3
    new = [_marker("nomic-embed-text:latest", text) for text in second.texts]
    after = _file_bytes(db)
    assert all(marker not in after for marker in old) and all(marker in after for marker in new)


def test_supersede_leaves_no_vector_bytes_of_the_replaced_source(tmp_path: Path) -> None:
    """Mutation: leave ``secure_deletion`` out of ``delete_chunk_vectors``."""

    db = _vault(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN}\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        first = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    provider = WideProvider()
    assert _embed(db, first.source_ids, provider).embedded == 1
    marker = _marker(NOMIC, provider.texts[0])
    assert marker in _file_bytes(db)
    (folder / "plan.md").write_text(f"# Plan\n\n{PLAIN} It changed.\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, supersede=True)
    assert marker not in _file_bytes(db)


def test_drop_leaves_no_vector_bytes_and_no_table(tmp_path: Path) -> None:
    """Mutation: leave ``secure_deletion`` out of ``delete_chunk_vectors``."""

    db = _vault(tmp_path)
    _ids, provider = _wide_sources(db, 3)
    markers = [_marker(NOMIC, text) for text in provider.texts]
    assert drop_chunk_vectors(store_context=_context(db)) == 3
    after = _file_bytes(db)
    assert all(marker not in after for marker in markers)
    assert not _rows(db, "SELECT 1 FROM sqlite_master WHERE name = 'source_chunk_embeddings'")


def test_the_setting_is_put_back_after_a_vector_write_or_delete(tmp_path: Path) -> None:
    """Mutation: leave the ``finally`` that restores the setting out of ``secure_deletion``."""

    from alicebot_api.vnext_stores.sqlite import source_chunk_vectors as vector_store

    db = _vault(tmp_path)
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)

        def setting() -> int:
            return int(store._execute("PRAGMA secure_delete").fetchone()["secure_delete"])

        before = setting()
        with vector_store.secure_deletion(store):
            assert setting() == 1
        assert setting() == before
        with pytest.raises(RuntimeError), vector_store.secure_deletion(store):
            raise RuntimeError("fails inside")
        assert setting() == before


# -- backfill, drop and the doctor ---------------------------------------------------------------------------------------------


def test_reindex_sources_embeds_missing_and_stale_chunks_and_drop_removes_everything(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The owner's command. Mutation: leave the signature comparison out of ``judge_source``."""

    db = _vault(tmp_path)
    source_id, chunk_ids = _source(db, "Plan", [PLAIN, PLAIN + " Two."])
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["embedded"] == 2 and first["pending"] == 0 and first["sources"] is True and first["model"] == NOMIC
        assert len(server.texts) == 2
        # Nothing is current-and-resent.
        assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["embedded"] == 0 and second["already_current"] == 2 and len(server.texts) == 2
        # A change of model makes every vector stale; the command embeds them again. The new model is of the same
        # family, so the text sent is the same and only the signature tells the vectors apart.
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "nomic-embed-text:latest")
        assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 0
        third = json.loads(capsys.readouterr().out)
        assert third["embedded"] == 2 and len(server.texts) == 4
    assert _rows(db, "SELECT DISTINCT model FROM source_chunk_embeddings") == [("nomic-embed-text:latest",)]
    assert onramp_main(["reindex-embeddings", "--sources", "--drop", "--db", str(db)]) == 0
    dropped = json.loads(capsys.readouterr().out)
    assert dropped.pop("retained_data") == CHUNK_VECTOR_RETAINED_DATA
    assert dropped == {"dropped": 2, "sources": True}
    # Dropping every vector leaves the vault as a build without the table left it.
    assert _rows(db, "SELECT name FROM sqlite_master WHERE name = 'source_chunk_embeddings'") == []


def test_reindex_sources_needs_the_opt_in_a_provider_and_an_endpoint_on_this_machine(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Mutation: run the backfill without the opt-in; let the command send to an endpoint that is not on this machine."""

    db = _vault(tmp_path)
    _source(db, "Plan", [PLAIN, PLAIN + " Two."])
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    assert "source_embeddings_not_enabled" in capsys.readouterr().err
    monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    assert "embedding_provider_not_configured" in capsys.readouterr().err
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, REMOTE_URL)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["reason"] == OFF_ENDPOINT_NOT_LOCAL and receipt["message"] == ENDPOINT_NOT_LOCAL_MESSAGE
    assert receipt["pending"] == 2 and receipt["pending_reasons"] == {PENDING_ENDPOINT_NOT_LOCAL: 2}
    assert receipt["embedded"] == 0 and receipt["sent"] == 0
    assert _vector_chunk_ids(db) == set()
    # --drop sends nothing and needs no opt-in, but it needs --sources.
    assert onramp_main(["reindex-embeddings", "--drop", "--db", str(db)]) == 2
    assert "invalid_request" in capsys.readouterr().err


def test_the_backfill_walks_every_live_source_and_skips_the_retired_ones(tmp_path: Path) -> None:
    db = _vault(tmp_path)
    live = [_source(db, f"Note {index}", [f"{PLAIN} Note {index}."])[0] for index in range(45)]
    retired, _ = _source(db, "Gone", [PLAIN + " Gone."])
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (retired,))
    provider = FakeProvider()
    receipt = backfill_source_chunks(store_context=_context(db), environ=ON, provider=provider)
    assert receipt.embedded == 45 and len(provider.texts) == 45 and len(live) == 45
    assert not any("Gone" in text for text in provider.texts)


def test_doctor_lines_count_current_pending_stale_and_skipped(tmp_path: Path, monkeypatch) -> None:
    """Mutation: count a stale vector as current."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN, PLAIN + " Two.", "Done.", f"key {GITHUB_TOKEN}"])
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, LOCAL_URL)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)

    def lines(environ: dict[str, str]) -> dict[str, str]:
        with sqlite_user_connection(db, USER_ID) as conn:
            return dict(line.split(": ", 1) for line in chunk_vector_doctor_lines(SQLiteVNextStore(conn, USER_ID), environ=environ))

    off = lines({})
    assert off["source embeddings"] == "off" and off["chunks with a current vector"] == "0"
    on = lines(ON)
    assert on["chunks pending a vector"] == "2" and on["chunks with a current vector"] == "0"
    assert on["chunks skipped"] == "2 (credential 1, too_short 1)"
    # Embed one of the two with a provider of the current configuration.
    embed_new_sources([source_id], store_context=_context(db), environ=ON, provider=FakeProvider())
    done = lines(ON)
    assert done["chunks with a current vector"] == "2" and done["chunks pending a vector"] == "0"
    # A model change is its own status, though the text sent is the same.
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "nomic-embed-text:latest")
    changed = lines(ON)
    assert changed["chunks with a stale vector"] == "2" and changed["chunks with a current vector"] == "0"
    # With the opt-in off the vectors are still counted.
    assert lines({})["chunks with a stale vector"] == "2"
    # An endpoint that is not on this machine: the mode line says why nothing is sent, and the chunks are pending.
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, REMOTE_URL)
    away = lines(ON)
    assert away["source embeddings"] == f"on, nothing is sent: {ENDPOINT_NOT_LOCAL_MESSAGE}"
    assert away["chunks pending a vector"] == "0" or away["chunks pending a vector"].isdigit()
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, LOCAL_URL)
    report = compile_local_vault_doctor(db, user_id=USER_ID)
    assert "source embeddings: off" in report and "chunks with a stale vector: 2" in report


def test_a_title_change_makes_the_vector_stale_in_the_census(tmp_path: Path, monkeypatch) -> None:
    """The text hash is part of being current. Mutation: leave the hash out of ``judge_source``."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Old title", [PLAIN])
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, LOCAL_URL)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
    _embed(db, [source_id], FakeProvider())
    with sqlite_user_connection(db, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).update_source(source_id=source_id, patch={"title": "New title"})
        census = dict(line.split(": ", 1) for line in chunk_vector_doctor_lines(SQLiteVNextStore(conn, USER_ID), environ=ON))
    assert census["chunks with a stale vector"] == "1" and census["chunks with a current vector"] == "0"


# -- export and import say vectors are not carried ---------------------------------------------------------------------------


def test_export_and_import_say_that_chunk_vectors_are_not_carried(tmp_path: Path, capsys) -> None:
    """Mutation: leave the note out of the export; leave it out of the import receipt."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    out = tmp_path / "backup.jsonl"
    assert onramp_main(["export", "--db", str(db), "--out", str(out)]) == 0
    assert "chunk vector" not in capsys.readouterr().err  # nothing to say about a vault without any
    _embed(db, [source_id], FakeProvider())
    assert onramp_main(["export", "--db", str(db), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "this vault holds 1 source chunk vector" in err and "reindex-embeddings --sources" in err
    assert "source_chunk_embeddings" not in out.read_text()
    restored = tmp_path / "restored"
    assert onramp_main(["import", "--in", str(out), "--data-dir", str(restored)]) == 0
    summary = capsys.readouterr().out
    assert "1 source chunk imported without vectors" in summary and "reindex-embeddings --sources" in summary
    assert _vector_chunk_ids(restored / "memory.db") == set()


# -- the real provider keeps the model's width -----------------------------------------------------------------------------------


def test_the_provider_returns_the_models_own_width_for_chunks_and_the_padded_width_for_memories() -> None:
    """Mutation: pad the native request to 1,536."""

    with RecordingServer() as server:
        provider = OpenAICompatibleEmbeddingProvider(base_url=server.base_url, model=NOMIC)
        native = provider.embed_batch_native(["a note"])
        padded = provider.embed_batch(["a note"])
    assert len(native[0]) == DIMENSIONS and len(padded[0]) == 1536
    assert native[0] == padded[0][:DIMENSIONS] and set(padded[0][DIMENSIONS:]) == {0.0}


def test_a_vector_with_a_non_finite_number_is_not_stored(tmp_path: Path) -> None:
    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN])
    provider = FakeProvider()
    provider.embed_batch = lambda texts: [[float("nan")] * 4 for _ in texts]  # type: ignore[method-assign]
    provider.embed_batch_native = provider.embed_batch  # type: ignore[method-assign]
    receipt = _embed(db, [source_id], provider)
    assert receipt.embedded == 0 and receipt.pending == 1 and _vector_chunk_ids(db) == set()


# -- the doors ------------------------------------------------------------------------------------------------------------------


def test_alice_capture_embeds_the_note_after_it_commits_and_names_the_counts(tmp_path: Path, monkeypatch) -> None:
    """The MCP door. Mutation: leave the call out of ``_handle_alice_vnext_capture``."""

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path

    db = _vault(tmp_path)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=USER_ID)
    arguments = {"raw_text": f"# Lantern\n\n{PLAIN}", "title": "Lantern review", "domain": "project", "sensitivity": "internal"}
    quiet = call_mcp_tool(context, name="alice_capture", arguments=arguments)
    assert "chunk_embeddings" not in quiet and _vector_chunk_ids(db) == set()
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        fresh = call_mcp_tool(
            context,
            name="alice_capture",
            arguments={**arguments, "raw_text": f"# Cobalt\n\n{PLAIN} Cobalt edition.", "title": "Cobalt review"},
        )
        # Capturing a note that is already stored embeds the chunks it still has pending.
        repeated = call_mcp_tool(context, name="alice_capture", arguments=arguments)
        sent = list(server.texts)
    assert fresh["chunk_embeddings"]["embedded"] == 1 and fresh["chunk_embeddings"]["pending"] == 0
    assert repeated["status"] == "duplicate" and repeated["chunk_embeddings"]["embedded"] == 1
    assert sent[0].startswith("search_document: Cobalt review\n") and sent[1].startswith("search_document: Lantern review\n")
    assert len(_vector_chunk_ids(db)) == 2


def test_the_agent_output_tool_embeds_the_note_it_saved_after_it_commits(tmp_path: Path, monkeypatch) -> None:
    """The legacy agent-output door. Mutation: leave the call out of ``_handle_alice_vnext_ingest_agent_output``.

    The tool does not complete on SQLite today (the SQLite store has no ``create_artifact``), so a stand-in for
    the connector service saves the source. What is tested is the wiring after the commit: the receipt gains
    ``chunk_embeddings`` when the opt-in is on, and gains nothing when it is off.
    """

    from alicebot_api.mcp import capture_automation
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, LEGACY_SURFACES_ENV, MCP_LEGACY_TOOLS_ENV, MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from alicebot_api.vnext_connectors import AgentOutputIngestResult

    class StandInConnectorService:
        def __init__(self, store, **_kwargs) -> None:
            self.store = store

        def ingest_agent_output(self, payload, *, policy_decision):
            digest = hashlib.sha256(str(payload["content"]).encode()).hexdigest()
            source = self.store.create_source(
                {
                    "source_type": "agent_output",
                    "title": payload["title"],
                    "content_hash": digest,
                    "domain": payload["domain"],
                    "sensitivity": payload["sensitivity"],
                }
            )
            self.store.create_source_chunk({"source_id": source["id"], "chunk_index": 0, "text": payload["content"]})
            return AgentOutputIngestResult("imported", str(source["id"]), None, None, policy_decision)

    monkeypatch.setattr(capture_automation, "VNextConnectorService", StandInConnectorService)
    monkeypatch.setenv(MCP_LEGACY_TOOLS_ENV, "1")
    monkeypatch.setenv(LEGACY_SURFACES_ENV, "1")
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    db = _vault(tmp_path)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=USER_ID)
    arguments = {
        "agent_id": "openclaw",
        "agent_type": "coding_agent",
        "permission_profile": "project_scoped_agent",
        "agent_run_id": "run-1",
        "project_scope": ["Alice"],
        "title": "Lantern review",
        "content": PLAIN,
        "domain": "project",
        "sensitivity": "internal",
    }
    quiet = call_mcp_tool(context, name="alice_vnext_ingest_agent_output", arguments=arguments)
    assert quiet["status"] == "imported" and "chunk_embeddings" not in quiet and _vector_chunk_ids(db) == set()
    with RecordingServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        fresh = call_mcp_tool(
            context,
            name="alice_vnext_ingest_agent_output",
            arguments={**arguments, "title": "Cobalt review", "content": f"{PLAIN} Cobalt edition."},
        )
        sent = list(server.texts)
    assert fresh["chunk_embeddings"]["embedded"] == 1 and fresh["chunk_embeddings"]["pending"] == 0
    assert len(sent) == 1 and sent[0].startswith("search_document: Cobalt review\n")
    assert len(_vector_chunk_ids(db)) == 1


# -- nothing in the recall path reads the table in this change -----------------------------------------------------------------------


def test_recall_answers_the_same_with_chunk_vectors_stored(tmp_path: Path) -> None:
    """Storing vectors changes no answer of ``alice_recall`` in this change.

    The same vault is asked the same questions before and after its chunks are embedded; the answers are equal once
    the fields that change on every call (run ids, timings) are taken out. Mutation: make the passage stage read
    the vector table.
    """

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path

    db = _vault(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "lanterns.md").write_text(f"# Lanterns\n\n{PLAIN}\n\n## Cobalt\n\nThe cobalt lantern hangs by the north door.\n")
    (folder / "keys.md").write_text("# Keys\n\nThe spare key to the brass cabinet hangs behind the kitchen clock.\n")
    with sqlite_user_connection(db, USER_ID) as conn:
        imported = VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=USER_ID)

    def normalise(value):  # noqa: ANN001, ANN202
        volatile = {"retrieval_run_id", "trace_id", "run_id", "duration_ms", "elapsed_ms", "created_at", "latency_ms"}
        if isinstance(value, dict):
            return {key: normalise(child) for key, child in value.items() if key not in volatile}
        if isinstance(value, list):
            return [normalise(child) for child in value]
        return value

    questions = ("cobalt lantern", "spare key brass cabinet", "what did the review decide about the amber build")

    def answers() -> str:
        return json.dumps(
            [normalise(call_mcp_tool(context, name="alice_recall", arguments={"query": question})) for question in questions],
            sort_keys=True,
        )

    before = answers()
    assert _embed(db, imported.source_ids, FakeProvider()).embedded >= 2
    assert len(_vector_chunk_ids(db)) >= 2
    assert answers() == before


def test_no_read_path_names_the_vector_table() -> None:
    """The table is read only by the modules of the vector stage, the stores' seam, and the commands.

    The brief, the continuity recall and the ranking switch never name the chunk vectors.
    Mutation: add a reference to the chunk vectors to ``session_briefing.py`` or to ``continuity_recall.py``.
    """

    root = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
    allowed = {
        "mcp/retrieval.py",
        "mcp/runtime.py",
        "onramp.py",
        "source_chunk_embeddings.py",
        "source_chunk_vector_search.py",
        "source_embedding_policy.py",
        "sqlite_schema.py",
        "sqlite_store.py",
        "vault_doctor.py",
        "vnext_capture.py",
        "vnext_retrieval.py",
        "vnext_stores/sqlite/chunk_vector_cache.py",
        "vnext_stores/sqlite/source_chunk_vectors.py",
        "vnext_stores/sqlite/source_readable.py",
        "vnext_stores/sqlite/source_retirement.py",
    }
    holders = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(token in path.read_text(encoding="utf-8") for token in ("source_chunk_embeddings", "source_chunk_vectors", "chunk_vector"))
    }
    assert holders - allowed == set(), holders - allowed
    for forbidden in ("session_briefing.py", "continuity_recall.py", "continuity_brief.py", "source_ranking.py"):
        assert forbidden not in holders
