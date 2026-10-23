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
import sqlite3
import threading
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api.onramp import _write_export, bootstrap_database, main as onramp_main
from alicebot_api.source_chunk_embeddings import (
    PENDING_CHANGED,
    PENDING_ENDPOINT_FAILED,
    PENDING_TIME_BUDGET,
    PENDING_VAULT_CAP,
    ChunkEmbeddingReceipt,
    backfill_source_chunks,
    chunk_vector_doctor_lines,
    embed_new_sources,
    sqlite_store_context,
)
from alicebot_api.source_embedding_policy import (
    OFF_DRY_RUN,
    OFF_NO_PROVIDER,
    OFF_REMOTE_NOT_ALLOWED,
    SKIP_CREDENTIAL,
    SKIP_LINK_ONLY,
    SKIP_SENSITIVITY_CEILING,
    SKIP_SOURCE_RETIRED,
    SKIP_TOO_SHORT,
    SOURCE_EMBEDDINGS_ENV,
    SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
    SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
    SOURCE_EMBEDDINGS_REMOTE_ENV,
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

    def __init__(self, *, status: int = 200) -> None:
        self.texts: list[str] = []
        self.status = status
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

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

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
        SOURCE_EMBEDDINGS_REMOTE_ENV,
        SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
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
REMOTE_ON = {SOURCE_EMBEDDINGS_ENV: "on", SOURCE_EMBEDDINGS_REMOTE_ENV: "on"}


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
    assert not source_embedding_settings({SOURCE_EMBEDDINGS_REMOTE_ENV: "on"}).enabled


def test_the_ceiling_defaults_to_internal_and_a_bad_value_falls_back_to_it() -> None:
    """Mutation: let an unknown ceiling value mean no ceiling."""

    assert source_embedding_settings({}).max_sensitivity == "internal"
    assert source_embedding_settings({SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV: " Private "}).max_sensitivity == "private"
    assert source_embedding_settings({SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV: "everything"}).max_sensitivity == "internal"


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
        ("http://127.1:11434/v1", True),
        ("http://2130706433/v1", True),
        ("http://[::1]:8080/v1", True),
        ("http://[::ffff:127.0.0.1]/v1", True),
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
    """Mutation: read a host name that is not ``localhost`` as loopback; read ``0.0.0.0`` as loopback."""

    assert endpoint_is_loopback(url) is expected


def test_a_proxy_that_would_carry_the_request_makes_a_loopback_endpoint_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy would take the text off the machine. Mutation: leave the proxy check out of ``endpoint_is_loopback``."""

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy.example.test:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    assert endpoint_is_loopback("http://127.0.0.1:11434/v1") is False
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    assert endpoint_is_loopback("http://127.0.0.1:11434/v1") is True


def test_the_posture_names_why_nothing_may_be_sent() -> None:
    """Mutation: let the posture allow a remote endpoint without its own opt-in."""

    off = source_embedding_settings({})
    assert egress_posture(off, provider_configured=True, base_url=LOCAL_URL).reason == "source_embeddings_off"
    on = source_embedding_settings(ON)
    assert egress_posture(on, provider_configured=False).reason == OFF_NO_PROVIDER
    local = egress_posture(on, provider_configured=True, base_url=LOCAL_URL)
    assert local.may_send and not local.remote and local.ceiling_rank is None
    remote = egress_posture(on, provider_configured=True, base_url=REMOTE_URL)
    assert not remote.may_send and remote.reason == OFF_REMOTE_NOT_ALLOWED
    allowed = egress_posture(source_embedding_settings(REMOTE_ON), provider_configured=True, base_url=REMOTE_URL)
    assert allowed.may_send and allowed.remote and allowed.ceiling == "internal"


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


# -- no send without the opt-in; remote needs its own ---------------------------------------------------------------------


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


def test_a_remote_endpoint_needs_its_own_opt_in(tmp_path: Path) -> None:
    """Mutation: let ``ALICE_SOURCE_EMBEDDINGS=on`` alone send to an endpoint that is not on this machine."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Plan", [PLAIN], sensitivity="public")
    provider = FakeProvider(base_url=REMOTE_URL)
    receipt = _embed(db, [source_id], provider)
    assert provider.requests == [] and receipt.reason == OFF_REMOTE_NOT_ALLOWED and receipt.embedded == 0
    allowed = _embed(db, [source_id], provider, environ=REMOTE_ON)
    assert allowed.embedded == 1 and len(provider.texts) == 1


def test_a_remote_endpoint_gets_only_sources_at_or_under_the_ceiling(tmp_path: Path) -> None:
    """The ceiling defaults to internal; ``unknown`` is held above it; ``private`` raises it for the three levels below.

    Mutation: remove the ceiling check from ``skip_reason``; rank ``unknown`` with ``internal``.
    """

    db = _vault(tmp_path)
    levels = ("public", "internal", "unknown", "private", "confidential", "highly_sensitive", "sacred", "regulated")
    ids = {level: _source(db, level, [f"{PLAIN} ({level})"], sensitivity=level)[0] for level in levels}
    provider = FakeProvider(base_url=REMOTE_URL)
    receipt = _embed(db, list(ids.values()), provider, environ=REMOTE_ON)

    def sent_levels() -> set[str]:
        return {level for level in levels if any(f"({level})" in text for text in provider.texts)}

    assert sent_levels() == {"public", "internal"}
    assert receipt.skipped[SKIP_SENSITIVITY_CEILING] == 6 and receipt.embedded == 2
    provider.requests.clear()
    _embed(db, list(ids.values()), provider, environ={**REMOTE_ON, SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV: "private"})
    assert sent_levels() == {"unknown", "private"}  # the two already embedded are current and not sent again
    provider.requests.clear()
    _embed(db, list(ids.values()), provider, environ={**REMOTE_ON, SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV: "regulated"})
    assert sent_levels() == {"confidential", "highly_sensitive", "sacred", "regulated"}


def test_the_ceiling_does_not_apply_to_an_endpoint_on_this_machine(tmp_path: Path) -> None:
    """Mutation: apply the ceiling to a loopback endpoint."""

    db = _vault(tmp_path)
    source_id, _ = _source(db, "Journal", [PLAIN], sensitivity="highly_sensitive")
    provider = FakeProvider()
    assert _embed(db, [source_id], provider).embedded == 1


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
    assert _rows(db, "SELECT value FROM alice_schema_state WHERE key = 'source_chunk_embeddings_version'") == [("1",)]
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
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert receipt["chunk_vectors"] == 2
    assert _vector_chunk_ids(db) == set(other_chunks)


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
    assert dropped == {"dropped": 2, "sources": True}
    # Dropping every vector leaves the vault as a build without the table left it.
    assert _rows(db, "SELECT name FROM sqlite_master WHERE name = 'source_chunk_embeddings'") == []


def test_reindex_sources_needs_the_opt_in_a_provider_and_for_a_remote_endpoint_its_own_opt_in(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Mutation: run the backfill without the opt-in."""

    db = _vault(tmp_path)
    _source(db, "Plan", [PLAIN], sensitivity="public")
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    assert "source_embeddings_not_enabled" in capsys.readouterr().err
    monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    assert "embedding_provider_not_configured" in capsys.readouterr().err
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, REMOTE_URL)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, NOMIC)
    assert onramp_main(["reindex-embeddings", "--sources", "--db", str(db)]) == 1
    assert "source_embeddings_remote_not_allowed" in capsys.readouterr().err
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
    """The table is read only by the modules of this change (store seam, lifecycle, doctor, commands).

    Mutation: add a reference to the chunk vectors to ``vnext_retrieval.py`` or to ``mcp/retrieval.py``.
    """

    root = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
    allowed = {
        "mcp/runtime.py",
        "onramp.py",
        "source_chunk_embeddings.py",
        "source_embedding_policy.py",
        "sqlite_schema.py",
        "sqlite_store.py",
        "vault_doctor.py",
        "vnext_capture.py",
        "vnext_stores/sqlite/source_chunk_vectors.py",
        "vnext_stores/sqlite/source_retirement.py",
    }
    holders = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(token in path.read_text(encoding="utf-8") for token in ("source_chunk_embeddings", "source_chunk_vectors", "chunk_vector"))
    }
    assert holders - allowed == set(), holders - allowed
    for forbidden in ("vnext_retrieval.py", "mcp/retrieval.py", "source_ranking.py", "continuity_recall.py"):
        assert forbidden not in holders
