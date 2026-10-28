#!/usr/bin/env python3
"""alice_bench: measure what an agent reads when it searches an imported notes folder.

This is the measurement harness of the search-quality release. It builds a fresh
vault from a folder of Markdown files with the importer the checkout ships, runs
``alice_recall`` the way an MCP host would, and scores what came back against
anchors: verbatim strings that a correct answer needs.

Commands, all of which take ``--run-dir`` (a directory this harness owns):

* ``build``: import a corpus folder into a fresh vault under the run directory, in
  a chosen order, and write the corpus snapshot that the grep arm searches.
* ``recall``: print the text ``alice_recall`` returns for one query.
* ``batch``: run every question of a question set, both query variants, and save
  the outputs with a fingerprint of the run.
* ``anchors``: check that every anchor of a question set occurs verbatim in the
  snapshot of a built vault.
* ``search``: the budgeted per-arm command an answering agent runs. Three searches,
  a file-locked counter and an append-only log.
* ``score``: Tier 1. Free and deterministic. Prints how many questions have all
  their anchors in the first 4 KB and 8 KB of what the agent would read, beside a
  negative control.
* ``fingerprint``: print the fingerprint of a run.

Tier 1 is necessary and not sufficient. An anchor in the output is stricter than
"the right file came back" and weaker than a correct answer.

The harness calls private internals of the checkout (``MCPServer._handle_request``,
the importer's snapshot reader), so a refactor of those fails a test in CI.
POSIX only: the search counter uses ``fcntl``.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import importlib
import io
import json
import os
import platform
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from alicebot_api.vnext_agent_control import AgentIdentity

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATES_PATH = REPO_ROOT / "gates.json"

QUESTIONS_SCHEMA = "alice-bench-questions/1"
OUTPUTS_SCHEMA = "alice-bench-outputs/1"
MANIFEST_SCHEMA = "alice-bench-run/1"
RUN_MARKER = ".alice_bench_run"
VAULT_DIRNAME = "vault"
VAULT_FILENAME = "memory.db"
SNAPSHOT_DIRNAME = "snapshot"
STATE_DIRNAME = "state"
MANIFEST_FILENAME = "manifest.json"
# One line per search of a vectors run, in the state folder: the status of the vector stage of that recall.
VECTOR_STATUS_FILENAME = "vector_status.jsonl"
# The count of the chunk vectors a readiness check made, kept in the run folder (``vector_census_key``).
VECTOR_CENSUS_FILENAME = "vector_census.json"
VECTOR_CENSUS_SCHEMA = "alice-bench-vector-census/1"

# Every variable whose name starts with this is removed from a run. The prefix
# covers ALICE_* (the product switches, the embeddings endpoint, the agent key)
# and ALICEBOT_* (the settings of the API process).
ENV_PREFIX = "ALICE"
SEARCH_QUALITY_ENV = "ALICE_SEARCH_QUALITY"
# The values --search-quality takes. ``vectors`` is passage mode plus the vectors of the source chunks, which
# exist only in a vault whose build embedded them (``embed_vault_chunks``), so a run of that value carries the
# three variables below and is held to the readiness checks of ``require_vector_readiness``.
SEARCH_QUALITY_CHOICES = ("off", "passage", "vectors", "on")
VECTORS = "vectors"
SOURCE_EMBEDDINGS_ENV = "ALICE_SOURCE_EMBEDDINGS"
EMBEDDINGS_BASE_URL_ENV = "ALICE_EMBEDDINGS_BASE_URL"
EMBEDDINGS_MODEL_ENV = "ALICE_EMBEDDINGS_MODEL"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text:v1.5"

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
VARIANTS = ("verbatim", "keyword")

RECALL_TOOL = "alice_recall"

# The Alice arm keeps these three at the tool's own defaults. The wrapper sends
# only the query and refuses any other value, so the claim can name the setting.
# A call that leaves one out gets the tool's default, which is what the
# fingerprint records for it; what a call really carried is recorded as sent.
PINNED_RECALL_DEFAULTS: Mapping[str, object] = {
    "limit": 8,
    "context_depth": "low",
    "include_sources": True,
}

# A fingerprint reads its recall settings off the calls a session sent. A session that
# has sent none yet (the ``fingerprint`` command opens a fresh one) sends this one, an
# ordinary word, through the same path every recall takes.
FINGERPRINT_PROBE_QUERY = "notes"

# Tier 1 scores text that came out of the vault and nothing else. Leaves are
# named by a path in which each list index is written "[]".
SCORED_LEAVES = frozenset({"sources[].excerpt", "results[].text"})
# Every other string leaf a recall can carry is metadata, the echoed question or
# an id. A leaf in neither list stops the scorer, so a new field has to be
# decided on purpose. The debug trace ("retrieval") is deliberately absent: the
# Alice arm never asks for it.
UNSCORED_LEAVES = frozenset(
    {
        "framing",
        "query",
        "results[].id",
        "results[].type",
        "results[].domain",
        "results[].status",
        "results[].writer.id",
        "results[].writer.established",
        "results[].validity.valid_from",
        "results[].validity.valid_to",
        "results[].validity.superseded_by_memory_id",
        "results[].validity.supersedes_memory_id",
        "results[].validity.corrected_at",
        "sources[].id",
        "sources[].source_type",
        "sources[].title",
        "sources[].captured_at",
        "sources[].domain",
        "sources[].sensitivity",
        "sources[].excerpt_kind",
        "sources[].current_memory_id",
        "sources[].writer.id",
        "sources[].writer.established",
        "entities[].id",
        "entities[].name",
        "entities[].entity_type",
    }
)

EXIT_OK = 0
EXIT_SEARCH_ERROR = 1
EXIT_REFUSED = 2
EXIT_BUDGET = 3

GREP_TIMEOUT_SECONDS = 30
_GREP_LETTERS = frozenset("inwxEFlLcohHvsIrR")
_GREP_COUNT_OPTIONS = frozenset({"-A", "-B", "-C", "-m"})


class BenchError(Exception):
    """A harness failure whose message is safe to print."""


class CheckoutError(BenchError):
    pass


class RunDirError(BenchError):
    pass


class AnchorError(BenchError):
    pass


class UnclassifiedLeafError(BenchError):
    pass


class PinnedSettingError(BenchError):
    pass


class BudgetExhausted(BenchError):
    pass


class GrepOptionError(BenchError):
    pass


# --------------------------------------------------------------------------
# Small helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to one space. Quoting flattens newlines."""

    return " ".join(text.split())


def unquote_leaf(value: str) -> str:
    """Undo the quoting layer of a recall excerpt.

    Recall wraps stored text in JSON quotes after it flattens whitespace, so a
    quote character in the source arrives as a backslash and a quote. The
    scorer compares the text the file holds, so it reads the layer back off.
    """

    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            inner = json.loads(value)
        except ValueError:
            return value
        if isinstance(inner, str):
            return inner
    return value


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------
# Gates


@dataclass(frozen=True)
class Gates:
    data: dict[str, Any]
    sha256: str
    path: Path

    @property
    def budgets(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.data["tier1"]["byte_budgets"])

    @property
    def gate_budget(self) -> int:
        return int(self.data["tier1"]["gate_budget_bytes"])

    @property
    def searches_per_run(self) -> int:
        return int(self.data["search"]["searches_per_run"])

    @property
    def grep_cap(self) -> int:
        return int(self.data["search"]["grep_cap_bytes"])

    @property
    def anchor_min_length(self) -> int:
        return int(self.data["tier1"]["anchor_min_length"])

    @property
    def anchor_max_files(self) -> int:
        return int(self.data["tier1"]["anchor_max_files"])

    @property
    def import_orders(self) -> tuple[str, ...]:
        return tuple(str(value) for value in self.data["import_orders"])

    @property
    def prompt_hashes(self) -> dict[str, str | None]:
        prompts = self.data["hashes"]["prompts"]
        return {str(key): (None if value is None else str(value)) for key, value in prompts.items()}


def load_gates(path: Path | None = None) -> Gates:
    target = DEFAULT_GATES_PATH if path is None else path
    raw = target.read_bytes()
    data = json.loads(raw)
    if data.get("schema") != "alice-bench-gates/1":
        raise BenchError(f"{target.name} is not an alice-bench-gates/1 file")
    return Gates(data=data, sha256=sha256_bytes(raw), path=target)


# --------------------------------------------------------------------------
# Environment


def scrub_environment(env: Mapping[str, str], *, allow: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``env`` with every ALICE* variable removed, then the arm's own set.

    An agent key or an embeddings endpoint in the parent must never reach a run:
    a key changes who the caller is, and an endpoint changes what a recall does.
    """

    clean = {name: value for name, value in env.items() if not name.startswith(ENV_PREFIX)}
    clean.update(allow or {})
    return clean


@contextlib.contextmanager
def scoped_environment(allow: Mapping[str, str] | None = None) -> Iterator[list[str]]:
    """Scrub ``os.environ`` for the length of the block and restore it after.

    Yields the sorted names that were removed. Never the values.
    """

    saved = dict(os.environ)
    removed = sorted(name for name in saved if name.startswith(ENV_PREFIX))
    os.environ.clear()
    os.environ.update(scrub_environment(saved, allow=allow))
    try:
        yield removed
    finally:
        os.environ.clear()
        os.environ.update(saved)


def require_local_endpoint(url: str) -> str:
    """A loopback Ollama address. A cloud host is refused before any call."""

    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or host not in LOOPBACK:
        raise BenchError("the model endpoint must be a loopback Ollama address")
    return url.strip().rstrip("/")


def embedding_settings(base: str, model: str) -> dict[str, str]:
    """The env Alice's embedding client reads. Ollama serves ``/v1/embeddings``."""

    local = require_local_endpoint(base)
    if not model.strip():
        raise BenchError("the embedding model must be set")
    return {
        EMBEDDINGS_BASE_URL_ENV: local + "/v1",
        EMBEDDINGS_MODEL_ENV: model.strip(),
    }


def vector_environment(settings: Mapping[str, str]) -> dict[str, str]:
    """The variables a vectors run carries besides the search quality: the opt-in, the endpoint and the model.

    ``settings`` is what ``embedding_settings`` returns and a build records as ``embedding_env``: the endpoint and the
    model, nothing else. The endpoint has to be on this machine, here as well as in the product, so a record that was
    edited to name another host is refused before a run starts.
    """

    if set(settings) != {EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV}:
        raise BenchError("a vectors run carries the embeddings endpoint and model, and no other setting")
    base = require_local_endpoint(str(settings[EMBEDDINGS_BASE_URL_ENV]))
    model = str(settings[EMBEDDINGS_MODEL_ENV]).strip()
    if not model:
        raise BenchError("the embedding model must be set")
    return {SOURCE_EMBEDDINGS_ENV: "on", EMBEDDINGS_BASE_URL_ENV: base, EMBEDDINGS_MODEL_ENV: model}


def arm_environment(search_quality: str | None, *, vector_settings: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only ALICE variables a run may carry.

    The search quality, and for a ``vectors`` run its embedding variables (``vector_environment``). A run of any
    other quality never carries them, so the setting of a vectors run cannot reach a passage or an off run.
    ``vector_settings`` left out gives a vectors run the quality alone, which is all that asking the product for a
    ranking mode needs; a run that recalls carries the settings.
    """

    env = {} if search_quality is None else {SEARCH_QUALITY_ENV: search_quality}
    if vector_settings is not None:
        if search_quality != VECTORS:
            raise BenchError("only a run of search quality vectors carries embedding settings")
        env.update(vector_environment(vector_settings))
    return env


@contextlib.contextmanager
def applied_environment(env: Mapping[str, str]) -> Iterator[None]:
    """Set ``env`` for the length of the block and put every name back as it was after."""

    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def resolved_ranking_mode(search_quality: str | None) -> str:
    """``document``, ``passage`` or ``vectors``: the ranking ``alice_recall`` runs on the SQLite vault this harness builds.

    ``search_quality`` is the value the run sets in ``ALICE_SEARCH_QUALITY``; ``None`` leaves it unset. An unset
    variable does not name one ranking: v0.20.2 ranks sources by document, and v0.21.0 ranks them by passage on
    SQLite. So the mode is not read off the variable. It is asked of the recall edge of the checkout that was
    imported (``recall_source_ranking``), under the environment this run carries, with a SQLite store and no
    agent key, which is how this harness calls recall. A checkout without that edge ranks by document whatever
    the variable says. ``on`` is the improved query, which the product refuses at the stage; it resolves to the
    passage mode it asks for. ``vectors`` is its own mode, passage mode plus the chunk vectors, and it is recorded
    as ``vectors``: a checkout that cannot rank by vectors (no edge, or an edge that answers with another mode) is
    refused, and so is any other value that resolves to ``vectors``, so a mode is never recorded for a ranking the
    run did not get.
    """

    try:
        module = importlib.import_module("alicebot_api.mcp.retrieval")
    except ModuleNotFoundError as exc:
        if exc.name != "alicebot_api.mcp.retrieval":
            raise
        return _checked_ranking_mode("document", search_quality)
    edge = getattr(module, "recall_source_ranking", None)
    if edge is None:
        return _checked_ranking_mode("document", search_quality)
    from alicebot_api.sqlite_store import SQLiteVNextStore

    with scoped_environment(arm_environment(search_quality)):
        try:
            # The edge reads the class of the store and the environment, so an instance with no connection is enough.
            ranking = edge(SQLiteVNextStore.__new__(SQLiteVNextStore), None)
        except ValueError as exc:
            raise BenchError(str(exc)) from exc
    return _checked_ranking_mode(str(ranking.mode), search_quality)


def _checked_ranking_mode(mode: str, search_quality: str | None) -> str:
    if (search_quality == VECTORS) != (mode == VECTORS):
        raise BenchError(
            f"search quality {search_quality or 'unset'} resolved to the {mode} ranking in this checkout, "
            "so the run would not be what its record says"
        )
    return mode


# --------------------------------------------------------------------------
# The chunk vectors of a vectors run
#
# ``--search-quality vectors`` ranks the passages of a source with the vectors of its chunks. Those exist only if
# the build made them: the product embeds the chunks of a source after an import when ``ALICE_SOURCE_EMBEDDINGS``
# is ``on`` and the embeddings endpoint is on this machine, and ``alice-memory reindex-embeddings --sources`` makes
# the ones an import left pending. A recall that finds no usable vector answers as passage mode does, so a vectors run
# that skipped any of this would be a passage run under another name. Everything below is how the harness refuses
# that: the build embeds through the product and fails unless nothing is pending, every search checks the vault
# again, and every recall reports the status of the vector stage so a search that did not use vectors can be counted.

# How many times the backfill is run before the build gives up. A pass that embeds nothing ends it earlier.
BACKFILL_PASSES = 5
DIGEST_TIMEOUT_SECONDS = 10
DIGEST_MAX_BYTES = 4_000_000


def accepted_skip_reasons() -> tuple[str, ...]:
    """The reasons the product documents for never sending a chunk, other than a retired source.

    A chunk whose text the credential check flags, a chunk of links and labels, and a chunk too short to read
    are skipped by design and counted by reason. A source that is deleted, replaced or scrubbed is the fourth
    documented reason (``source_retired``); a vault that was just built has none, so it is not accepted here and a
    build that finds one fails.
    """

    from alicebot_api.source_embedding_policy import SKIP_CREDENTIAL, SKIP_LINK_ONLY, SKIP_TOO_SHORT

    return (SKIP_CREDENTIAL, SKIP_LINK_ONLY, SKIP_TOO_SHORT)


def read_model_digest(settings: Mapping[str, str]) -> str:
    """The digest Ollama reports for the embedding model, read from the loopback endpoint (``/api/tags``)."""

    base = require_local_endpoint(str(settings[EMBEDDINGS_BASE_URL_ENV]))
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    model = str(settings[EMBEDDINGS_MODEL_ENV]).strip()
    names = {model, model + ":latest"} if ":" not in model else {model}
    # No proxy of the environment may carry this request, whatever the variables say.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(root + "/api/tags", timeout=DIGEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read(DIGEST_MAX_BYTES))
    except (OSError, ValueError) as exc:
        raise BenchError(f"the model digest cannot be read from the loopback endpoint ({type(exc).__name__})") from exc
    entries = body.get("models") if isinstance(body, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and names & {str(entry.get("name")), str(entry.get("model"))}:
            digest = entry.get("digest")
            if isinstance(digest, str) and digest.strip():
                return digest.strip()
    raise BenchError(f"the loopback endpoint lists no digest for the model {model}")


def _stored_vector_dimensions(db_path: Path) -> list[int]:
    """The distinct widths of the chunk vectors the vault stores, read without writing to it."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "source_chunk_embeddings" not in tables:
            return []
        rows = conn.execute("SELECT DISTINCT dimensions FROM source_chunk_embeddings ORDER BY dimensions").fetchall()
    finally:
        conn.close()
    return [int(row[0]) for row in rows]


def chunk_vector_state(db_path: Path) -> dict[str, Any]:
    """What the product counts for the chunk vectors of the vault, under the environment now in force.

    The census is the product's own (``alice-memory doctor`` prints it): ``current`` chunks have a vector made
    with today's signature from today's text, ``pending`` ones have none and may be sent, ``stale`` ones have a
    vector of another model, endpoint, rule or text, and ``skipped`` ones are never sent, by reason. It reads every
    live chunk and sends nothing.
    """

    from alicebot_api.source_chunk_embeddings import census_egress, chunk_vector_census, sqlite_store_context

    egress = census_egress()
    signature = egress.signature
    if egress.provider is None or signature is None:
        raise BenchError("no embedding provider is configured for this run")
    with sqlite_store_context(db_path, DEFAULT_USER_ID) as store:
        census = chunk_vector_census(store, egress=egress)
    return {
        "current": census.current,
        "pending": census.pending,
        "stale": census.stale,
        "stored": census.stored,
        "skipped": dict(sorted(census.skipped.items())),
        "dimensions": _stored_vector_dimensions(db_path),
        "signature": asdict(signature),
    }


def _run_backfill(db_path: Path) -> dict[str, Any]:
    """``alice-memory reindex-embeddings --sources`` on the vault, in process. Returns its receipt and exit code."""

    from alicebot_api import onramp

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = onramp.main(["reindex-embeddings", "--sources", "--db", str(db_path)])
    receipt: dict[str, Any] = {"exit_code": code}
    for text in (stdout.getvalue(), stderr.getvalue()):
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            try:
                parsed = json.loads(lines[-1])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                receipt.update(parsed)
    return receipt


def embed_vault_chunks(db_path: Path, settings: Mapping[str, str]) -> dict[str, Any]:
    """Make the chunk vectors of a built vault through the product and return the ``chunk_vectors`` record.

    The caller has already imported with the vectors environment, so the product embedded each source as it
    arrived. This runs the product's backfill until nothing is pending (or a pass makes no progress), then counts
    the vault with the product's census and refuses the build unless every chunk is accounted for: none pending, none
    stale, no skip other than the documented ones (``accepted_skip_reasons``), at least one vector, and one width.
    The record names the model, the digest Ollama reports for it, the width, the counts and the signature fields the
    product stamps on a vector.
    """

    env = vector_environment(settings)
    with applied_environment(env):
        receipt: dict[str, Any] = {}
        passes = 0
        for passes in range(1, BACKFILL_PASSES + 1):
            receipt = _run_backfill(db_path)
            if not receipt.get("pending") and not receipt.get("reason") and not receipt.get("error"):
                break
            if not receipt.get("embedded"):
                break
        state = chunk_vector_state(db_path)
    problems: list[str] = []
    if state["pending"]:
        reasons = receipt.get("pending_reasons")
        why = (
            ", ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))
            if isinstance(reasons, dict)
            else receipt.get("reason") or (receipt.get("error") or {}).get("code")
        )
        problems.append(f"{state['pending']} chunks are still pending a vector" + (f" ({why})" if why else ""))
    if state["stale"]:
        problems.append(f"{state['stale']} chunks hold a vector of another model, endpoint or rule")
    unaccepted = {reason: n for reason, n in state["skipped"].items() if reason not in accepted_skip_reasons()}
    if unaccepted:
        listed = ", ".join(f"{reason} {n}" for reason, n in sorted(unaccepted.items()))
        problems.append(f"chunks were skipped for a reason a fresh build does not have ({listed})")
    if not state["stored"]:
        problems.append("no chunk was embedded")
    if len(state["dimensions"]) > 1:
        problems.append("the stored vectors do not share one width")
    if problems:
        raise BenchError("the chunk vectors are incomplete: " + "; ".join(problems))
    return {
        "model": str(settings[EMBEDDINGS_MODEL_ENV]).strip(),
        "model_digest": read_model_digest(settings),
        "dimensions": state["dimensions"][0],
        "chunks_embedded": state["stored"],
        "pending": state["pending"],
        "stale": state["stale"],
        "skipped": state["skipped"],
        "skipped_total": sum(state["skipped"].values()),
        "signature": state["signature"],
        "backfill_passes": passes,
    }


def require_vector_environment(settings: Mapping[str, str]) -> None:
    """Refuse a process that does not carry the vectors environment its run recorded.

    A recall under a missing opt-in, endpoint or model answers as passage mode does and says so only in its trace,
    so this is checked before the run is read, not left to the status of each call.
    """

    expected = {SEARCH_QUALITY_ENV: VECTORS, **vector_environment(settings)}
    missing = sorted(name for name, value in expected.items() if os.environ.get(name) != value)
    if missing:
        raise BenchError("the vectors environment is not applied to this run (" + ", ".join(missing) + ")")


def vector_census_key(db_path: Path, recorded: Mapping[str, Any]) -> dict[str, Any]:
    """What a count of the chunk vectors is true for, read from the vault in a few queries and without writing to it.

    The product's own stamp (``source_chunk_vector_stamp``, which its triggers rewrite when a vector is overwritten or
    deleted) does not move when a vector is added, because the product finds additions by row number. So the key also
    holds the number of vector rows and the highest row number, the same two numbers for the chunks and the sources,
    how many sources are live and how many are scrubbed, the signature of the provider in force, and the digest the
    build recorded. A vector deleted, overwritten or added, a note imported or retired, and a change of model or
    endpoint each change the key. An edit of the text of a chunk in place, with no write to any of those, does not;
    the commands that read a vault whole (``require_matching_vault``) still see that.
    """

    from alicebot_api.source_embedding_policy import chunk_signature
    from alicebot_api.sqlite_schema import source_chunk_vectors_stamp
    from alicebot_api.vnext_embeddings import get_embedding_provider

    provider = get_embedding_provider()
    if provider is None:
        raise BenchError("no embedding provider is configured for this run")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        stamp = source_chunk_vectors_stamp(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        vectors = (
            conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM source_chunk_embeddings").fetchone()
            if "source_chunk_embeddings" in tables
            else (0, 0)
        )
        chunks = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM source_chunks").fetchone()
        sources = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0), COALESCE(SUM(deleted_at IS NULL), 0), "
            "COALESCE(SUM(COALESCE(json_extract(metadata_json, '$.scrubbed'), 0) = 1), 0) FROM sources"
        ).fetchone()
    finally:
        conn.close()
    return {
        "stamp": stamp or "",
        "vectors": [int(value) for value in vectors],
        "chunks": [int(value) for value in chunks],
        "sources": [int(value) for value in sources],
        "signature": asdict(chunk_signature(provider)),
        "model_digest": recorded.get("model_digest"),
    }


def _cached_vector_census(cache_path: Path | None, key: Mapping[str, Any]) -> dict[str, Any] | None:
    """The count kept in ``cache_path`` when it was made for ``key``, else None. A file that cannot be read is no cache."""

    if cache_path is None:
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(cached, dict) and cached.get("schema") == VECTOR_CENSUS_SCHEMA and cached.get("key") == key:
        state = cached.get("state")
        return dict(state) if isinstance(state, dict) else None
    return None


def _store_vector_census(cache_path: Path | None, key: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    """Keep ``state`` for ``key``. The file is replaced whole, so a search running at the same time reads either one."""

    if cache_path is None:
        return
    document = {"schema": VECTOR_CENSUS_SCHEMA, "key": key, "state": state}
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, cache_path)
    except OSError:
        temporary.unlink(missing_ok=True)


def require_vector_readiness(
    db_path: Path,
    recorded: Mapping[str, Any] | None,
    settings: Mapping[str, str],
    *,
    cache_path: Path | None = None,
) -> None:
    """Refuse to read a vectors run whose vault no longer holds the vectors its build recorded.

    The environment has to be the run's (``require_vector_environment``). The product's census must then find
    nothing pending and nothing stale, as many stored vectors as the build embedded, one width, and the signature
    the build recorded. The digest Ollama reports for the model must still be the recorded one, because a model
    pulled again under the same name embeds a query where the stored vectors do not live. Each is a refusal and none
    is repaired. Every message is one line.

    The census reads every live chunk, so with a ``cache_path`` it is made once and kept for the key of
    ``vector_census_key``; a later check that finds the same key uses it and reads the vault in a few queries. What
    the build recorded is compared on every check, and the digest is asked of the endpoint on every check.
    """

    if not isinstance(recorded, Mapping) or not isinstance(recorded.get("chunks_embedded"), int):
        raise BenchError("this vectors run recorded no chunk vectors; build it again with --embeddings on")
    require_vector_environment(settings)
    key = vector_census_key(db_path, recorded) if cache_path is not None else None
    state = _cached_vector_census(cache_path, key) if key is not None else None
    if state is None:
        state = chunk_vector_state(db_path)
        if key is not None:
            _store_vector_census(cache_path, key, state)
    wrong: list[str] = []
    if state["pending"]:
        wrong.append(f"{state['pending']} chunks are pending a vector")
    if state["stale"]:
        wrong.append(f"{state['stale']} chunks hold a stale vector")
    if state["stored"] != recorded["chunks_embedded"] or state["current"] != recorded["chunks_embedded"]:
        wrong.append(
            f"the vault holds {state['stored']} vectors, {state['current']} of them current, "
            f"and the build recorded {recorded['chunks_embedded']}"
        )
    if state["dimensions"] != [recorded.get("dimensions")]:
        wrong.append("the stored vectors are not of the recorded width")
    if state["signature"] != recorded.get("signature"):
        wrong.append("the vectors signature differs from the build's")
    if wrong:
        raise BenchError("this vectors run is not ready: " + "; ".join(wrong))
    if read_model_digest(settings) != recorded.get("model_digest"):
        raise BenchError("this vectors run is not ready: the model digest differs from the build's")


def split_vector_trace(text: str) -> tuple[str, dict[str, Any]]:
    """The text a recall without ``debug`` would have returned, and the status of its vector stage.

    A call made with ``trace=True`` (``alice_arm_arguments``) carries ``debug``, whose only effect is a ``retrieval``
    block at the end of the result. This removes that block and serializes the rest as the server does, so the text
    an answering agent sees is the one it would have had without the flag. The status is the stage's own record
    (``status``, ``readable_chunks``, ``stale_chunks``, ``discovered_in_head``, ``reordered_slots``,
    ``filler_picks``) with ``used`` true when the stage ran. A trace that holds no stage record is
    reported as such and never as ``enabled``.
    """

    from alicebot_api.recall_framing import serialize_mcp_tool_result

    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return text, {"status": "unknown: the recall result is not JSON", "used": False}
    retrieval = payload.pop("retrieval", None)
    stage = None
    if isinstance(retrieval, dict):
        stages = retrieval.get("stages")
        sources = stages.get("sources") if isinstance(stages, dict) else None
        stage = sources.get("vector") if isinstance(sources, dict) else None
    if isinstance(stage, dict) and isinstance(stage.get("status"), str):
        status: dict[str, Any] = {key: value for key, value in stage.items() if isinstance(value, str | int)}
        status["used"] = stage["status"] == "enabled"
    else:
        status = {"status": "unknown: the recall trace holds no vector stage", "used": False}
    return serialize_mcp_tool_result(payload), status


def format_chunk_vectors(block: Mapping[str, Any]) -> str:
    """One line for the build output: what a vectors build embedded."""

    skipped = ", ".join(f"{reason} {count}" for reason, count in sorted(block["skipped"].items()))
    return (
        f"chunk vectors embedded {block['chunks_embedded']}, pending {block['pending']}, "
        f"skipped {block['skipped_total']}" + (f" ({skipped})" if skipped else "")
        + f", model {block['model']}, digest {block['model_digest']}"
    )


def vector_stage_summary(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How many recalls there were, how many ran the vector stage, and the count of each status."""

    statuses: dict[str, int] = {}
    for call in calls:
        statuses[str(call["status"])] = statuses.get(str(call["status"]), 0) + 1
    return {
        "calls": len(calls),
        "used": sum(1 for call in calls if call.get("used") is True),
        "statuses": dict(sorted(statuses.items())),
    }


# --------------------------------------------------------------------------
# The checkout under test


def src_dir(repo: Path) -> Path:
    return repo / "apps" / "api" / "src"


def activate_checkout(repo: Path) -> Path:
    """Put the checkout's source first on the path and prove the import came from it.

    The venv of a worktree usually imports the main checkout, so the path is set
    here and the real path of ``alicebot_api.__file__`` is checked afterwards.
    Returns that real path.
    """

    repo = repo.resolve()
    source = src_dir(repo)
    if not (source / "alicebot_api" / "__init__.py").is_file():
        raise CheckoutError("the checkout has no apps/api/src/alicebot_api package")
    if "alicebot_api" not in sys.modules:
        sys.path.insert(0, str(source))
    import alicebot_api

    actual = Path(os.path.realpath(alicebot_api.__file__))
    if not actual.is_relative_to(source.resolve()):
        raise CheckoutError(
            "alicebot_api was already imported from outside the checkout; "
            "start a new process with --checkout"
        )
    return actual


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckoutError(f"the checkout has a .git and git could not be run on it ({type(exc).__name__})") from exc


# What the manifest and the fingerprint say when a checkout has no .git of its own, as an exported
# copy of a commit does. It is a plain statement, never a failure: the content hash identifies the code.
NO_GIT = "no git"
WITH_GIT = "present"


def git_state(repo: Path) -> dict[str, object]:
    """The commit of the checkout and whether it differs from that commit, when it has a .git.

    Dirty means a tracked file differs from HEAD, or an untracked file sits under
    apps/ or workers/, because either changes what the product code does.

    A ``.git`` is looked for at the root of the checkout itself, as a folder or as the file a git
    worktree has, and nowhere above it: ``git -C`` would walk up into any repository that happens
    to contain an exported copy and report that repository's commit as the copy's own. A checkout
    with no ``.git`` there is reported as ``no git`` with no commit and no dirty flag. A ``.git``
    that git cannot read a commit from is a refusal, because a commit that cannot be read is a
    commit that is silently missing from the record.

    Having a ``.git`` is not enough, because git walks past one it finds invalid (an empty folder,
    a folder with only a ``HEAD`` stub, a symlink that points nowhere) and answers from the repository
    around it. So the root git reports has to be the checkout's own root, and anything else is a
    refusal too.
    """

    if not os.path.lexists(repo / ".git"):
        return {"git": NO_GIT, "git_sha": None, "dirty": None}
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CheckoutError("the checkout has a .git that git cannot read a commit from")
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(repo):
        raise CheckoutError(
            "the checkout has a .git that is not the root of a repository of its own "
            "(git answers from the repository around it), so its commit cannot be recorded"
        )
    tracked = _git(repo, "diff", "--quiet", "HEAD")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "--", "apps", "workers")
    dirty = tracked.returncode != 0 or bool(untracked.stdout.strip())
    return {"git": WITH_GIT, "git_sha": head.stdout.strip(), "dirty": dirty}


# The only parts of the source tree that are not hashed. A ``__pycache__`` folder is where the
# interpreter writes compiled copies of the files beside it, and a harness run writes some, so a
# hash that read it would differ between the build and the next command. A compiled file that
# sits outside a ``__pycache__`` folder is importable on its own, so it is hashed like any other.
_SOURCE_HASH_SKIPPED_DIRS = frozenset({"__pycache__"})
_SOURCE_HASH_SKIPPED_FILES = frozenset({".DS_Store"})


def checkout_source_sha256(repo: Path) -> str:
    """A hash of the code a run imports: every file under ``apps/api/src``, in a stable order.

    The hash covers each file's path (POSIX, relative to ``apps/api/src``) and its bytes, each
    written after its length so that no two trees can give the same stream. It needs no git, so an
    exported copy of a commit and a worktree of the same commit give the same value, and it moves
    with every edit, which a commit and a dirty flag cannot do once the checkout is dirty. A symlink
    in the tree is refused, because its target is not part of the tree. Installed dependencies
    and the files outside ``apps/api/src`` are not covered.
    """

    root = src_dir(repo.resolve())
    if not root.is_dir():
        raise CheckoutError("the checkout has no apps/api/src to hash")
    found: dict[str, Path] = {}
    for current, dirnames, filenames in os.walk(root):
        base = Path(current)
        kept: list[str] = []
        for name in dirnames:
            if base.joinpath(name).is_symlink():
                raise CheckoutError(f"apps/api/src holds a symlink ({name}), which the source hash cannot follow")
            if name not in _SOURCE_HASH_SKIPPED_DIRS:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = base / name
            if name in _SOURCE_HASH_SKIPPED_FILES:
                continue
            if path.is_symlink():
                raise CheckoutError(f"apps/api/src holds a symlink ({name}), which the source hash cannot follow")
            if not path.is_file():
                raise CheckoutError(f"apps/api/src holds {name}, which is not a plain file the source hash can read")
            found[path.relative_to(root).as_posix()] = path
    digest = hashlib.sha256()
    for relative in sorted(found):
        try:
            data = found[relative].read_bytes()
        except OSError as exc:
            raise CheckoutError(f"apps/api/src/{relative} cannot be read ({type(exc).__name__})") from exc
        name_bytes = relative.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def harness_sha256() -> str:
    """The hash of this script as it is on disk now. It chooses what is imported and how a result is scored."""

    return sha256_file(Path(__file__))


# The hash of this script as Python loaded it, which is the code that every command of this process runs. A
# command is one process, so this is the hash at the start of the command, read before any of its work and
# before ``main`` parses a flag. A harness edited while a command runs, or after the script was loaded and
# before a build began, differs from it on disk.
LOADED_HARNESS_SHA256 = harness_sha256()


def require_harness_unchanged(current: str | None = None) -> None:
    """Refuse when the harness on disk is not the one this process loaded. ``current`` is a hash already read."""

    if (harness_sha256() if current is None else current) != LOADED_HARNESS_SHA256:
        raise BenchError(
            "the harness script changed after this process loaded it (harness_sha256 differs), "
            "so what the command did is not the work of one version of it; run it again"
        )


# What says that two processes ran the same code: whether the checkout has a git history and, if so,
# its commit and whether it differs from that commit; a hash of the source it imports; the real path
# ``alicebot_api`` was imported from; the Python and SQLite it ran on, because the full-text ranking of
# a recall comes from SQLite; and the hash of this harness, which decides what a build imports and what a
# batch asks. The source hash is the one that cannot be fooled by a second edit on a dirty tree or by the
# absence of git. The harness hash is part of the record of a build so that a number can be held to the
# harness that made it, which a file cannot be if only the outputs name it.
CHECKOUT_IDENTITY_KEYS = (
    "git",
    "git_sha",
    "dirty",
    "checkout_source_sha256",
    "alicebot_api_file",
    "python_version",
    "sqlite_version",
    "harness_sha256",
)


def checkout_identity(repo: Path) -> dict[str, object]:
    """The identity of the checkout and the harness this process runs, in the keys of ``CHECKOUT_IDENTITY_KEYS``."""

    import alicebot_api

    state = git_state(repo)
    return {
        "git": state["git"],
        "git_sha": state["git_sha"],
        "dirty": state["dirty"],
        "checkout_source_sha256": checkout_source_sha256(repo),
        "alicebot_api_file": str(Path(os.path.realpath(alicebot_api.__file__))),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "harness_sha256": harness_sha256(),
    }


def differing_identity_keys(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """The keys of ``CHECKOUT_IDENTITY_KEYS`` on which two identities disagree."""

    return [key for key in CHECKOUT_IDENTITY_KEYS if recorded[key] != current[key]]


def require_matching_build(manifest: Mapping[str, Any] | None, repo: Path) -> Mapping[str, Any]:
    """Refuse to read a vault that another checkout or another harness built. Returns the manifest it checked.

    A vault is rebuilt for every checkout and never cached, because the chunker is one of
    the things a release can change. The manifest records which checkout built the vault,
    and a command that reads it has to be running the same one: the same source content,
    the same commit and dirty state when there is a git history, the same ``alicebot_api``
    on disk, and the same Python and SQLite. It also records the hash of the harness that
    built it (in the build record, and as ``harness_sha256`` of the manifest), and a different
    harness, which chooses what is imported and how, is refused too.
    """

    if manifest is None:
        raise RunDirError("the run directory holds no build; run build first")
    recorded = manifest.get("build")
    if not isinstance(recorded, dict) or any(key not in recorded for key in CHECKOUT_IDENTITY_KEYS):
        raise CheckoutError(
            "the manifest does not say which checkout built this vault (it may come from an older harness); "
            "build it again with --rebuild"
        )
    if manifest.get("harness_sha256") != harness_sha256():
        raise CheckoutError(
            "this vault was built by a different version of the harness (harness_sha256 differs); "
            "build it again with --rebuild"
        )
    differing = differing_identity_keys(recorded, checkout_identity(repo))
    if differing:
        raise CheckoutError(
            "this vault was built by a different checkout ("
            + ", ".join(differing)
            + " differ); a vault is rebuilt for every checkout, so build it again with --rebuild"
        )
    return manifest


# --------------------------------------------------------------------------
# Run directories


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def prepare_run_dir(run_dir: Path, *, create: bool) -> Path:
    """The resolved run directory. It is new, empty, or one this harness made."""

    resolved = run_dir.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise RunDirError("the run directory is not a directory")
        entries = list(resolved.iterdir())
        if entries and not (resolved / RUN_MARKER).exists():
            raise RunDirError("the run directory is not empty and is not a harness run directory")
        if create and not entries:
            (resolved / RUN_MARKER).write_text("alice_bench run directory\n", encoding="utf-8")
        return resolved
    if not create:
        raise RunDirError("the run directory does not exist")
    resolved.mkdir(parents=True)
    (resolved / RUN_MARKER).write_text("alice_bench run directory\n", encoding="utf-8")
    return resolved


def require_run_dir(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    if not (resolved / RUN_MARKER).is_file():
        raise RunDirError("the run directory was not made by this harness")
    return resolved


def resolve_inside(run_dir: Path, requested: str | None, *, default: Path, what: str) -> Path:
    """``requested`` or ``default``, refused when it is outside the run directory."""

    target = default if requested is None else Path(requested)
    resolved = target.resolve()
    if not _is_inside(resolved, run_dir):
        raise RunDirError(f"{what} is outside the run directory")
    return resolved


# What the harness keeps in a run directory for itself. A vault may not sit on any of
# these, because a rebuild deletes the vault folder and must never take the marker, the
# manifest, the snapshot or an answering run's counter with it.
_RESERVED_RUN_ENTRIES = frozenset({SNAPSHOT_DIRNAME, STATE_DIRNAME, MANIFEST_FILENAME, RUN_MARKER})


def check_vault_location(run_dir: Path, data_dir: Path) -> Path:
    """``data_dir`` when it is a folder of its own inside ``run_dir``, else a refusal.

    It cannot be the run directory itself (a rebuild would delete the directory and its
    marker) and it cannot be, or sit under, a name the harness keeps for itself.
    """

    resolved = data_dir.resolve()
    if not _is_inside(resolved, run_dir):
        raise RunDirError("--data-dir is outside the run directory")
    if resolved == run_dir:
        raise RunDirError("--data-dir cannot be the run directory itself; name a folder inside it")
    reserved = resolved.relative_to(run_dir).parts[0]
    if reserved in _RESERVED_RUN_ENTRIES:
        raise RunDirError(f"--data-dir cannot be {reserved}, which the harness keeps for itself")
    return resolved


def vault_path(data_dir: Path) -> Path:
    return data_dir / VAULT_FILENAME


# --------------------------------------------------------------------------
# Corpus and build


@dataclass(frozen=True)
class CorpusFile:
    relative_path: str
    path: Path
    text: str
    raw_sha256: str


def read_corpus(corpus_dir: Path) -> tuple[Path, list[CorpusFile]]:
    """The Markdown files the importer would select, read once, sorted by path."""

    from alicebot_api.importer_paths import DEFAULT_MAX_TEXT_FILE_BYTES
    from alicebot_api.markdown_import import _snapshot_markdown_source

    try:
        folder, snapshot = _snapshot_markdown_source(corpus_dir, max_file_bytes=DEFAULT_MAX_TEXT_FILE_BYTES)
    except (OSError, ValueError) as exc:  # the importer's own refusal: no Markdown, a file too large, unreadable
        raise BenchError(f"the corpus folder cannot be read: {exc}") from exc
    files = [
        CorpusFile(
            relative_path=item.relative_path,
            path=item.path,
            text=item.text,
            raw_sha256=sha256_file(item.path),
        )
        for item in snapshot
    ]
    files.sort(key=lambda item: item.relative_path)
    if not files:
        raise BenchError("the corpus folder holds no Markdown files")
    return folder, files


def corpus_hash(files: Sequence[CorpusFile]) -> str:
    lines = [f"{item.relative_path}\0{item.raw_sha256}" for item in sorted(files, key=lambda f: f.relative_path)]
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def order_files(files: Sequence[CorpusFile], order: str) -> list[CorpusFile]:
    """Capture order: ``sorted``, ``reverse`` or ``shuffle:SEED``.

    The recency list of recall breaks ties by capture time, so the order a vault
    was built in can move which documents it lists. A result is shown under
    several orders for that reason.
    """

    ordered = sorted(files, key=lambda item: item.relative_path)
    if order == "sorted":
        return ordered
    if order == "reverse":
        return list(reversed(ordered))
    if order.startswith("shuffle:"):
        try:
            seed = int(order.split(":", 1)[1])
        except ValueError as exc:
            raise BenchError("shuffle needs an integer seed, as in shuffle:7") from exc
        random.Random(seed).shuffle(ordered)
        return ordered
    raise BenchError("order must be sorted, reverse or shuffle:SEED")


def _import_one(db_path: Path, file_path: Path, *, domain: str, sensitivity: str) -> dict[str, Any]:
    from alicebot_api import onramp

    stdout, stderr = io.StringIO(), io.StringIO()
    argv = [
        "import-markdown",
        "--db",
        str(db_path),
        "--from",
        str(file_path),
        "--domain",
        domain,
        "--sensitivity",
        sensitivity,
    ]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = onramp.main(argv)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    if code != 0 or not lines:
        raise BenchError(f"importing {file_path.name} failed (exit {code})")
    record = json.loads(lines[-1])
    if not isinstance(record, dict):
        raise BenchError(f"importing {file_path.name} printed no batch record")
    return record


def read_vault_sources(db_path: Path) -> list[dict[str, Any]]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT title, raw_path, external_id, metadata_json FROM sources "
            "WHERE deleted_at IS NULL ORDER BY captured_at, id"
        ).fetchall()
    finally:
        conn.close()
    sources: list[dict[str, Any]] = []
    for title, raw_path, external_id, metadata_json in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        sources.append(
            {
                "title": title,
                "raw_path": raw_path,
                "external_id": external_id,
                "metadata": metadata,
            }
        )
    return sources


def vault_row_counts(db_path: Path) -> dict[str, int]:
    """Row counts of every table, event_log included. A recall must change none."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts: dict[str, int] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    finally:
        conn.close()
    return counts


def vault_chunks_sha256(db_path: Path) -> str:
    """One hash over every stored chunk: its source, its position and its text.

    Chunk text is what a recall serves and what the snapshot is a copy of, so it is the quantity the
    harness measures. A line is the source's ``external_id`` (``None`` for a chunk whose source row is
    gone), the chunk's index and a hash of its text. The lines are sorted, so the value does not
    depend on the order rows were stored in, only on what is stored.
    """

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT s.external_id, c.chunk_index, c.text FROM source_chunks AS c "
            "LEFT JOIN sources AS s ON s.id = c.source_id AND s.user_id = c.user_id"
        ).fetchall()
    finally:
        conn.close()
    lines = sorted(
        json.dumps([external_id, chunk_index, sha256_bytes(str(text).encode("utf-8"))])
        for external_id, chunk_index, text in rows
    )
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def vault_sources_sha256(db_path: Path) -> str:
    """One hash over every column of every live source: its labels, its type, its dates, its metadata, all of it.

    A recall filters on a source's ``domain`` and ``sensitivity`` (a source above the ceiling, or in a held back
    domain, is never returned) and shows its title, type, capture date and metadata, so a vault built with other
    labels, or a column edited in place, returns other text from the same chunks. Which columns matter is not
    a short list (``metadata_json`` is free form), so no column is picked: each live row is written out whole,
    its columns by name, and the lines are sorted, so the value depends on what is stored and not on the order
    rows came back in. It reads the vault without writing to it. A source the vault has deleted is not live and is
    not read, as in every other record here; marking one deleted moves the count of sources.
    """

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        selected = conn.execute("SELECT * FROM sources WHERE deleted_at IS NULL")
        names = [column[0] for column in selected.description]
        rows = selected.fetchall()
    finally:
        conn.close()
    lines = sorted(json.dumps(dict(zip(names, row, strict=True)), sort_keys=True, default=repr) for row in rows)
    return sha256_bytes("\n".join(lines).encode("utf-8"))


# What a vault is held to at read time. Each is a record of the manifest and a value that
# ``vault_identity`` reads from the vault alone.
VAULT_IDENTITY_KEYS = (
    "sources",
    "chunks",
    "vault_text_sha256",
    "vault_sources_sha256",
    "vault_chunks_sha256",
    "vault_row_counts",
)


def vault_identity(db_path: Path) -> dict[str, Any]:
    """What the vault holds, read from the vault alone and without writing to it.

    The count of live sources, the count of chunks, a hash over the live sources in the order they
    were captured (each as its corpus path, its title and a hash of its stored text), a hash over every
    column of every live source (its labels included), a hash over every stored chunk (its source, its
    index and its text), and the row count of every table. A vault built from another corpus, in another
    import order, with other labels, or by a chunker that cut it differently gives another value, and so
    does a vault that has had a source column or a chunk edited in place or a memory, a graph row or an
    event added, which the manifest of the build can then be held to. A recall writes no row, so none of
    these moves while a vault is read.
    """

    sources = read_vault_sources(db_path)
    lines: list[str] = []
    for source in sources:
        text = source["metadata"].get("raw_text")
        if not isinstance(text, str):
            raise BenchError("a stored source holds no raw_text; the vault cannot be identified")
        lines.append(f"{source['external_id']}\0{source['title']}\0{sha256_bytes(text.encode('utf-8'))}")
    counts = vault_row_counts(db_path)
    return {
        "sources": len(sources),
        "chunks": counts.get("source_chunks", 0),
        "vault_text_sha256": sha256_bytes("\n".join(lines).encode("utf-8")),
        "vault_sources_sha256": vault_sources_sha256(db_path),
        "vault_chunks_sha256": vault_chunks_sha256(db_path),
        "vault_row_counts": counts,
    }


def require_matching_vault(manifest: Mapping[str, Any], run_dir: Path, data_dir: Path) -> None:
    """Refuse to read a vault, or a grep snapshot, that the manifest does not describe.

    The manifest records the folder the vault was built into, what that vault holds and what the
    snapshot holds. A command that reads either has to find exactly that: the same folder (a rebuild
    into another ``--data-dir`` leaves the old folder on disk), the same sources in the same capture
    order, the same columns in every source (the labels that decide what recall may return among them),
    the same chunks with the same text, the same number of rows in every table (a vault copied
    over another one under the same name, or one that has been edited or added to), and the same
    snapshot files, all of them, whatever their names. Each is a refusal and none is repaired,
    because the two arms must read the text the manifest names.
    """

    recorded_dir = manifest.get("vault_dir")
    if not isinstance(recorded_dir, str) or any(key not in manifest for key in (*VAULT_IDENTITY_KEYS, "snapshot_hash")):
        raise RunDirError(
            "the manifest does not say which vault folder it describes or what that vault holds "
            "(it may come from an older harness); build again with --rebuild"
        )
    current_dir = data_dir.relative_to(run_dir).as_posix()
    if recorded_dir != current_dir:
        raise RunDirError(
            f"this run was built into the vault folder {recorded_dir!r} and not {current_dir!r}; "
            "point --data-dir at the folder the manifest names, or build again with --rebuild"
        )
    db_path = vault_path(data_dir)
    if not db_path.is_file():
        raise RunDirError(f"the vault folder {current_dir!r} holds no {VAULT_FILENAME}; build again with --rebuild")
    actual = vault_identity(db_path)
    differing = [key for key in VAULT_IDENTITY_KEYS if actual[key] != manifest[key]]
    if differing:
        detail = ""
        if "vault_row_counts" in differing and isinstance(manifest["vault_row_counts"], dict):
            recorded_counts = manifest["vault_row_counts"]
            moved = sorted(
                name
                for name in {*recorded_counts, *actual["vault_row_counts"]}
                if recorded_counts.get(name) != actual["vault_row_counts"].get(name)
            )
            detail = "; rows moved in " + ", ".join(moved)
        raise RunDirError(
            "the vault does not hold what the manifest says it was built with ("
            + ", ".join(differing)
            + " differ"
            + detail
            + "); it was replaced or changed after the build, so build again with --rebuild"
        )
    snapshot_dir = run_dir / SNAPSHOT_DIRNAME
    if not snapshot_dir.is_dir() or snapshot_folder_hash(snapshot_dir) != manifest["snapshot_hash"]:
        raise RunDirError(
            "the grep snapshot does not hold what the manifest says it was built with; "
            "it was replaced or changed after the build, so build again with --rebuild"
        )


def require_visible_labels(*, domain: str, sensitivity: str) -> None:
    """Refuse labels that would hide the corpus from recall while grep still reads it.

    Both arms must read the same text. Recall answers a keyless caller up to the default
    sensitivity ceiling, so a file imported above it would be in the grep snapshot and
    absent from every recall. The sensitive domains are held back from a project brief,
    so they are refused the same way rather than left to chance. Both lists are read from
    the checkout under test, which keeps the harness from keeping a copy of its own.
    """

    from alicebot_api import vnext_agent_control, vnext_memory_commit

    ceiling = tuple(getattr(vnext_agent_control, "DEFAULT_AGENT_SENSITIVITY", ()))
    held_back = frozenset(getattr(vnext_memory_commit, "SENSITIVE_DOMAINS", ()))
    if not ceiling:
        raise CheckoutError("this checkout does not say what sensitivity recall answers by default")
    if sensitivity not in ceiling:
        raise BenchError(
            f"--sensitivity {sensitivity} is above what recall answers by default ({', '.join(ceiling)}), "
            "so grep would read files that recall cannot return"
        )
    if domain in held_back:
        raise BenchError(
            f"--domain {domain} is one of the sensitive domains a project view holds back, "
            "so the two arms might not read the same text"
        )


def build_vault(
    *,
    corpus_dir: Path,
    run_dir: Path,
    data_dir: Path,
    order: str,
    domain: str,
    sensitivity: str,
    rebuild: bool,
    repo: Path,
    search_quality: str | None = None,
    vector_settings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a fresh vault and the grep snapshot under ``run_dir``. Returns the manifest.

    The vault is never cached: the chunker is one of the things a release can
    change. Files are imported one at a time in the requested order, each by the
    same ``alice-memory import-markdown`` code a person would run. The manifest
    records which checkout built the vault, the folder it was built into and what the
    vault and the snapshot hold, and every later command refuses to read it from another
    checkout or to read a folder or a snapshot that does not match
    (``require_matching_build``, ``require_matching_vault``).

    The identity of the checkout and the harness is read twice, here before anything is imported and again
    after the last file is, and a build whose two readings differ is refused and records nothing. Read once,
    after the loop, it would name the tree as it ended up, and a source edited while the build ran would be
    recorded for a vault that the code before the edit had cut. The first reading also has to give the
    harness this process loaded, so a script edited before the build began is not recorded under its new hash.

    A build of search quality ``vectors`` takes ``vector_settings`` (the endpoint and model of
    ``embedding_settings``). Each file is then imported with the vectors environment, so the product embeds the
    chunks of each source as it arrives, and the product's backfill runs after the last one
    (``embed_vault_chunks``); the build fails unless nothing is left pending. The manifest records the
    settings as ``embedding_env`` and the result as ``chunk_vectors``. A build of any other quality takes
    no settings and records neither.
    """

    if (search_quality == VECTORS) != (vector_settings is not None):
        raise BenchError("a build of search quality vectors needs the embedding settings, and no other build takes them")
    identity = checkout_identity(repo)
    require_harness_unchanged(str(identity["harness_sha256"]))
    data_dir = check_vault_location(run_dir, data_dir)
    require_visible_labels(domain=domain, sensitivity=sensitivity)
    folder, files = read_corpus(corpus_dir)
    ordered = order_files(files, order)
    snapshot_dir = run_dir / SNAPSHOT_DIRNAME
    manifest_path = run_dir / MANIFEST_FILENAME
    existing = [path for path in (data_dir, snapshot_dir, manifest_path) if path.exists()]
    if existing and not rebuild:
        raise RunDirError("the run directory already holds a build; use --rebuild to replace it")
    for path in existing:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    data_dir.mkdir(parents=True)
    db_path = vault_path(data_dir)

    receipts: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    with applied_environment({} if vector_settings is None else vector_environment(vector_settings)):
        for item in ordered:
            record = _import_one(db_path, item.path, domain=domain, sensitivity=sensitivity)
            receipts.append(record)
            labels = [str(label) for label in record.get("skipped_credential_items", [])]
            if labels:
                withheld.append({"file": item.relative_path, "items": labels})
    chunk_vectors = None if vector_settings is None else embed_vault_chunks(db_path, vector_settings)
    snapshot_files = export_snapshot(db_path, snapshot_dir, corpus_root=folder if folder.is_dir() else folder.parent)
    held = vault_identity(db_path)
    moved = differing_identity_keys(identity, checkout_identity(repo))
    if moved:
        raise CheckoutError(
            "the checkout changed while the vault was being built (" + ", ".join(moved) + " differ), "
            "so the vault may have been cut by code that the record would not name; nothing was recorded, "
            "so build again with --rebuild"
        )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "built_at": utc_now(),
        "build": {
            **identity,
            "search_quality": search_quality if search_quality is not None else "unset",
            "ranking_mode": resolved_ranking_mode(search_quality),
        },
        "order": order,
        "vault_dir": data_dir.relative_to(run_dir).as_posix(),
        "capture_order": [entry["relative_path"] for entry in snapshot_files],
        "domain": domain,
        "sensitivity": sensitivity,
        "corpus_hash": corpus_hash(files),
        "corpus_file_count": len(files),
        "snapshot_hash": snapshot_hash({entry["relative_path"]: entry["text"] for entry in snapshot_files}),
        **{key: held[key] for key in VAULT_IDENTITY_KEYS},
        "withheld": withheld,
        "duplicates": sum(1 for record in receipts if int(record.get("imported_count", 0)) == 0),
        "harness_sha256": identity["harness_sha256"],
    }
    if vector_settings is not None:
        manifest["embedding_env"] = dict(vector_settings)
        manifest["chunk_vectors"] = chunk_vectors
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def export_snapshot(db_path: Path, snapshot_dir: Path, *, corpus_root: Path) -> list[dict[str, str]]:
    """Write the stored text of every live source under ``snapshot_dir``.

    The grep arm searches this folder, so it can never see a line the importer
    withheld from the vault. Returns the files in capture order.
    """

    root = corpus_root.resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []
    for source in read_vault_sources(db_path):
        text = source["metadata"].get("raw_text")
        if not isinstance(text, str):
            raise BenchError("a stored source holds no raw_text; the grep snapshot cannot be built")
        raw_path = source["raw_path"]
        try:
            relative = Path(str(raw_path)).resolve().relative_to(root).as_posix()
        except (ValueError, TypeError):
            relative = str(source["metadata"].get("relative_path") or source["external_id"] or source["title"])
        target = snapshot_dir / relative
        if not _is_inside(target.resolve(), snapshot_dir.resolve()):
            raise BenchError("a source path leaves the snapshot folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append({"relative_path": relative, "text": text})
    return written


def _snapshot_digest(file_hashes: Mapping[str, str]) -> str:
    lines = [f"{name}\0{digest}" for name, digest in sorted(file_hashes.items())]
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def snapshot_hash(files: Mapping[str, str]) -> str:
    return _snapshot_digest({name: sha256_bytes(text.encode("utf-8")) for name, text in files.items()})


def snapshot_folder_hash(snapshot_dir: Path) -> str:
    """``snapshot_hash`` of the files as they are on disk now, read as bytes.

    Bytes and not text, so that the hash never depends on how a reader treats line endings or
    encodings. For the folder a build wrote this is equal to the ``snapshot_hash`` the build recorded.
    """

    return _snapshot_digest(
        {
            path.relative_to(snapshot_dir).as_posix(): sha256_bytes(path.read_bytes())
            for path in snapshot_dir.rglob("*")
            if path.is_file()
        }
    )


def read_snapshot(snapshot_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file():
            files[path.relative_to(snapshot_dir).as_posix()] = path.read_text(encoding="utf-8")
    return files


# --------------------------------------------------------------------------
# Questions and anchors


@dataclass(frozen=True)
class Anchor:
    text: str
    file: str
    approved: str | None = None


@dataclass(frozen=True)
class Fact:
    label: str
    anchors: tuple[Anchor, ...]


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    keyword_query: str | None
    kind: str | None
    facts: tuple[Fact, ...]

    def query_for(self, variant: str) -> str:
        if variant == "verbatim":
            return self.question
        if variant == "keyword":
            if not self.keyword_query:
                raise BenchError(f"question {self.id} has no keyword query")
            return self.keyword_query
        raise BenchError(f"unknown variant {variant}")


@dataclass(frozen=True)
class QuestionSet:
    set_id: str
    questions: tuple[Question, ...]
    sha256: str


def load_questions(path: Path) -> QuestionSet:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != QUESTIONS_SCHEMA:
        raise BenchError(f"{path.name} is not an {QUESTIONS_SCHEMA} file")
    questions: list[Question] = []
    seen: set[str] = set()
    for entry in data.get("questions", []):
        qid = str(entry["id"])
        if qid in seen:
            raise BenchError(f"duplicate question id {qid}")
        seen.add(qid)
        facts: list[Fact] = []
        for fact in entry["facts"]:
            anchors = tuple(
                Anchor(text=str(a["text"]), file=str(a["file"]), approved=a.get("approved"))
                for a in fact["anchors"]
            )
            if not anchors or any(not anchor.text.strip() for anchor in anchors):
                raise BenchError(f"question {qid} has a fact with no usable anchor")
            facts.append(Fact(label=str(fact.get("fact", "")), anchors=anchors))
        if not facts:
            raise BenchError(f"question {qid} has no facts")
        questions.append(
            Question(
                id=qid,
                question=str(entry["question"]),
                keyword_query=(None if entry.get("keyword_query") is None else str(entry["keyword_query"])),
                kind=(None if entry.get("kind") is None else str(entry["kind"])),
                facts=tuple(facts),
            )
        )
    if not questions:
        raise BenchError("the question set is empty")
    return QuestionSet(set_id=str(data.get("set_id", path.stem)), questions=tuple(questions), sha256=sha256_bytes(raw))


def verify_anchors(qset: QuestionSet, snapshot: Mapping[str, str], gates: Gates) -> dict[str, Any]:
    """Check every anchor against the snapshot. Raises ``AnchorError`` on a miss.

    A miss means the anchor is not verbatim in the file it names, so a corpus
    change or a typo cannot pass quietly. The report also records, for each
    anchor, its length, how many files hold it and whether the question holds
    it, and flags the ones a person has to read.
    """

    normalized = {name: normalize_ws(text) for name, text in snapshot.items()}
    missing: list[str] = []
    rows: list[dict[str, Any]] = []
    for question in qset.questions:
        own_text = normalize_ws(question.question)
        own_keywords = normalize_ws(question.keyword_query or "")
        for fact_index, fact in enumerate(question.facts, start=1):
            for anchor in fact.anchors:
                text = normalize_ws(anchor.text)
                named = normalized.get(anchor.file)
                where = f"{question.id} fact {fact_index} in {anchor.file}"
                if named is None:
                    missing.append(f"{where}: no such file in the snapshot")
                    continue
                if text not in named:
                    missing.append(f"{where}: not verbatim")
                    continue
                files_holding = sum(1 for body in normalized.values() if text in body)
                flags: list[str] = []
                if len(text) < gates.anchor_min_length:
                    flags.append("short")
                if files_holding > gates.anchor_max_files:
                    flags.append("many_files")
                in_question = text in own_text
                in_keywords = text in own_keywords
                if in_question or in_keywords:
                    flags.append("in_question")
                rows.append(
                    {
                        "question": question.id,
                        "fact": fact_index,
                        "length": len(text),
                        "files": files_holding,
                        "in_question": in_question,
                        "in_keyword_query": in_keywords,
                        "flags": flags,
                        "approved": anchor.approved,
                    }
                )
    if missing:
        raise AnchorError("anchors that are not verbatim in the corpus snapshot:\n" + "\n".join(missing))
    flagged = [row for row in rows if row["flags"]]
    return {
        "anchors": len(rows),
        "flagged": len(flagged),
        "flagged_unapproved": sum(1 for row in flagged if not row["approved"]),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# Reading a recall: leaves, offsets, bytes


@dataclass(frozen=True)
class Leaf:
    path: str
    value: str
    end_byte: int


def locate_leaves(text: str) -> list[Leaf]:
    """Every string leaf of a recall result with the byte offset where it ends.

    The text is re-emitted in the order it was parsed and compared with the
    original, so an offset can only come from text that really has that shape.
    """

    parsed = json.loads(text)
    pieces: list[str] = []
    leaves: list[Leaf] = []
    position = 0

    def write(chunk: str) -> None:
        nonlocal position
        pieces.append(chunk)
        position += len(chunk.encode("utf-8"))

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            write("{")
            for index, (key, child) in enumerate(value.items()):
                if index:
                    write(",")
                write(json.dumps(key))
                write(":")
                walk(child, f"{path}.{key}" if path else str(key))
            write("}")
        elif isinstance(value, list):
            write("[")
            for index, child in enumerate(value):
                if index:
                    write(",")
                walk(child, f"{path}[]")
            write("]")
        elif isinstance(value, str):
            write(json.dumps(value))
            leaves.append(Leaf(path=path, value=value, end_byte=position))
        else:
            write(json.dumps(value))

    walk(parsed, "")
    if "".join(pieces) != text:
        raise BenchError("the recall text is not compact JSON in parse order; leaves cannot be placed")
    return leaves


def classify_leaves(leaves: Sequence[Leaf]) -> None:
    """Fail closed: every string leaf is scored or known not to be."""

    unknown = sorted({leaf.path for leaf in leaves if leaf.path not in SCORED_LEAVES and leaf.path not in UNSCORED_LEAVES})
    if unknown:
        raise UnclassifiedLeafError("unclassified recall leaf: " + ", ".join(unknown))


@dataclass(frozen=True)
class OutputScore:
    found: tuple[bool, ...]
    bytes_total: int
    documents: int
    passages: int

    @property
    def hit(self) -> bool:
        return all(self.found)


def score_output(text: str, facts: Sequence[Fact], *, budget: int) -> OutputScore:
    """Which facts have an anchor in a scored string that ends inside the first ``budget`` bytes.

    The budget counts the whole serialized output, because that is what an agent
    pays: the framing line, then every key in order, so a long ``entities`` list
    ahead of ``sources`` spends it.
    """

    leaves = locate_leaves(text)
    classify_leaves(leaves)
    readable = [
        normalize_ws(unquote_leaf(leaf.value))
        for leaf in leaves
        if leaf.path in SCORED_LEAVES and leaf.end_byte <= budget
    ]
    found = tuple(
        any(
            needle in body
            for anchor in fact.anchors
            if (needle := normalize_ws(anchor.text))
            for body in readable
        )
        for fact in facts
    )
    parsed = json.loads(text)
    sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
    source_ids = {str(entry.get("id")) for entry in sources if isinstance(entry, dict)}
    return OutputScore(
        found=found,
        bytes_total=len(text.encode("utf-8")),
        documents=len(source_ids),
        passages=len(sources),
    )


def score_set(
    outputs: Mapping[str, Mapping[str, str]],
    qset: QuestionSet,
    *,
    variant: str,
    budget: int,
) -> dict[str, Any]:
    """Tier 1 for one variant at one budget, with the negative control.

    The control scores each question's anchors against the next question's
    output (a rotation by one). A set whose floor is not near zero has a leak or
    weak anchors.
    """

    questions = qset.questions
    hits = 0
    facts_found = 0
    facts_total = 0
    floor_hits = 0
    bytes_sum = 0
    documents_sum = 0
    passages_sum = 0
    per_question: dict[str, bool] = {}
    for index, question in enumerate(questions):
        text = outputs[question.id][variant]
        result = score_output(text, question.facts, budget=budget)
        per_question[question.id] = result.hit
        hits += 1 if result.hit else 0
        facts_found += sum(1 for flag in result.found if flag)
        facts_total += len(question.facts)
        bytes_sum += result.bytes_total
        documents_sum += result.documents
        passages_sum += result.passages
        if len(questions) > 1:
            neighbour = questions[(index + 1) % len(questions)]
            control = score_output(outputs[neighbour.id][variant], question.facts, budget=budget)
            floor_hits += 1 if control.hit else 0
    count = len(questions)
    return {
        "n": count,
        "hits": hits,
        "facts_found": facts_found,
        "facts_total": facts_total,
        "floor_hits": floor_hits if count > 1 else None,
        "bytes_per_call": bytes_sum / count,
        "documents_per_call": documents_sum / count,
        "passages_per_call": passages_sum / count,
        "per_question": per_question,
    }


def score_outputs_document(doc: Mapping[str, Any], qset: QuestionSet, gates: Gates) -> dict[str, Any]:
    if doc.get("schema") != OUTPUTS_SCHEMA:
        raise BenchError(f"not an {OUTPUTS_SCHEMA} file")
    if doc.get("question_set_sha256") not in (None, qset.sha256):
        raise BenchError("the outputs were made for a different question set")
    outputs = doc["outputs"]
    for question in qset.questions:
        if question.id not in outputs:
            raise BenchError(f"the outputs hold nothing for question {question.id}")
    report: dict[str, Any] = {"set_id": qset.set_id, "variants": {}}
    for variant in VARIANTS:
        if not all(variant in outputs[q.id] for q in qset.questions):
            continue
        report["variants"][variant] = {
            str(budget): score_set(outputs, qset, variant=variant, budget=budget) for budget in gates.budgets
        }
    return report


def format_report(label: str, report: Mapping[str, Any], gates: Gates, *, per_question: bool) -> str:
    lines = [f"Tier 1, {label}, set {report['set_id']}"]
    lines.append(f"{'variant':9} {'budget':>7} {'hits':>9} {'facts':>11} {'floor':>9} {'bytes/call':>11} {'docs/call':>10}")
    for variant, by_budget in report["variants"].items():
        for budget in gates.budgets:
            cell = by_budget[str(budget)]
            floor = "n/a" if cell["floor_hits"] is None else f"{cell['floor_hits']}/{cell['n']}"
            lines.append(
                f"{variant:9} {budget:>7} {cell['hits']:>4}/{cell['n']:<4} "
                f"{cell['facts_found']:>5}/{cell['facts_total']:<5} {floor:>9} "
                f"{cell['bytes_per_call']:>11.0f} {cell['documents_per_call']:>10.2f}"
            )
    if per_question:
        for variant, by_budget in report["variants"].items():
            cell = by_budget[str(gates.gate_budget)]
            lines.append(f"per question, {variant}, {gates.gate_budget} bytes (dev only)")
            for qid, flag in cell["per_question"].items():
                lines.append(f"  {qid} {'hit' if flag else 'miss'}")
    return "\n".join(lines)


def minimum_across_orders(reports: Sequence[tuple[str, Mapping[str, Any]]], gates: Gates) -> dict[str, dict[str, int]]:
    """The lowest hit count per variant and budget over the import orders.

    The arm rule reads membership at 8 KB and never position, and it takes the
    minimum over the orders, because the order moves what the recency list shows.
    """

    minimums: dict[str, dict[str, int]] = {}
    for variant in VARIANTS:
        for budget in gates.budgets:
            values = [
                int(report["variants"][variant][str(budget)]["hits"])
                for _, report in reports
                if variant in report["variants"]
            ]
            if values:
                minimums.setdefault(variant, {})[str(budget)] = min(values)
    return minimums


# Fingerprint keys that must be equal in every outputs file of one comparison: the same
# checkout (its commit when it has one, and the hash of its source in any case), switch,
# corpus, snapshot, question set, gates and harness. Only the import
# order may differ, and it has to.
SAME_ACROSS_ORDERS = (
    "git_sha",
    "dirty",
    "alicebot_api_file",
    "checkout_source_sha256",
    "python_version",
    "sqlite_version",
    "tools_list_digest",
    "search_quality",
    "recall_arguments",
    "recall_limit",
    "recall_note_passage_budget",
    "recall_context_depth",
    "recall_include_sources",
    "corpus_hash",
    "snapshot_hash",
    "question_set_sha256",
    "gates_sha256",
    "harness_sha256",
)


def require_own_build_identity(name: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Refuse an outputs file that contradicts itself. Returns its fingerprint.

    A fingerprint holds the build record of the vault the run read (``vault_build``) and the identity of the
    run itself, and every key of the checkout identity, the harness hash included, has to be the same in both:
    the checkout and the harness that built the vault have to be the ones that ran it. This is a property of one
    file, so it is checked for each file whatever the number of files.
    """

    fp = document.get("fingerprint")
    if not isinstance(fp, dict):
        raise BenchError(f"{name} has no fingerprint, so what it measured cannot be checked")
    needed = dict.fromkeys((*SAME_ACROSS_ORDERS, *CHECKOUT_IDENTITY_KEYS, "import_order", "vault_build"))
    missing = [key for key in needed if key not in fp]
    if missing:
        raise BenchError(f"the fingerprint of {name} lacks {', '.join(missing)}")
    built = fp["vault_build"]
    if not isinstance(built, dict) or any(key not in built for key in CHECKOUT_IDENTITY_KEYS):
        raise BenchError(f"the fingerprint of {name} does not say which checkout built the vault it ran on")
    moved = differing_identity_keys(built, fp)
    if moved:
        raise BenchError(
            f"{name} ran on a vault that another checkout built ({', '.join(moved)} differ between the build "
            "record and the run), so its numbers name two checkouts"
        )
    return fp


def require_comparable_outputs(documents: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    """Refuse outputs that contradict themselves, and refuse to take a minimum over outputs that did not measure the same thing.

    Each file is held to itself first, however many there are (``require_own_build_identity``): the checkout
    that built its vault has to be the checkout that ran it, key for key. The minimum over import orders is
    only meaningful across one checkout, one switch, one corpus and one question set, so two or more files
    are then compared with each other. Two outputs files from different commits, or from one order twice,
    would give a number that names neither. A single file has nothing to be compared with.
    """

    fingerprints = [(name, require_own_build_identity(name, document)) for name, document in documents]
    if len(fingerprints) < 2:
        return
    first_name, first = fingerprints[0]
    for name, fp in fingerprints[1:]:
        differing = [key for key in SAME_ACROSS_ORDERS if fp[key] != first[key]]
        if differing:
            raise BenchError(
                f"{name} and {first_name} did not measure the same thing ({', '.join(differing)} differ); "
                "the minimum over import orders is read across one checkout, switch, corpus and question set"
            )
    orders = [str(fp["import_order"]) for _, fp in fingerprints]
    if len(set(orders)) != len(orders):
        raise BenchError("two outputs files come from the same import order; each one must come from its own")


def without_per_question(report: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``report`` with the per-question hits and misses left out.

    They are a dev-only view that a person asks for with ``--per-question``.
    """

    copy = json.loads(json.dumps(report))
    for by_budget in copy["variants"].values():
        for cell in by_budget.values():
            cell.pop("per_question", None)
    return dict(copy)


# --------------------------------------------------------------------------
# The MCP session


class McpSession:
    """The checkout's MCP server, called in process the way a host calls it over stdio.

    ``_handle_request`` is what the stdio loop calls for each message, so the text
    here is the text a host would put in front of the model.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        user_id: str = DEFAULT_USER_ID,
        agent_identity: AgentIdentity | None = None,
        trace_vectors: bool = False,
    ) -> None:
        from uuid import UUID

        from alicebot_api import onramp
        from alicebot_api.mcp_server import MCPServer
        from alicebot_api.mcp_tools import MCPRuntimeContext

        context = MCPRuntimeContext(
            database_url=onramp.sqlite_url_for_path(db_path),
            user_id=UUID(user_id),
            agent_identity=agent_identity,
            agent_identity_resolved=agent_identity is not None,
        )
        self._server = MCPServer(context=context, input_stream=io.BytesIO(), output_stream=io.BytesIO())
        self._next_id = 1
        # The arguments of every alice_recall call this session sent, as they reached the
        # server. The fingerprint reads the recall settings from here and from nothing else.
        self.recall_calls: list[dict[str, Any]] = []
        # A vectors run asks every recall for its trace (``debug``), takes the status of the vector stage out of it
        # and hands back the text a call without the trace returns. One status per call, in order.
        self.trace_vectors = trace_vectors
        self.vector_calls: list[dict[str, Any]] = []

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            request["params"] = params
            if method == "tools/call" and params.get("name") == RECALL_TOOL:
                self.recall_calls.append(copy.deepcopy(dict(params.get("arguments") or {})))
        self._next_id += 1
        response = self._server._handle_request(request)
        if response is None:
            raise BenchError(f"the server returned nothing for {method}")
        return response

    def tools_list(self) -> list[dict[str, Any]]:
        response = self._request("tools/list")
        tools = response["result"]["tools"]
        return [dict(tool) for tool in tools]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """``(is_error, text)``: the one text block the host would show the model."""

        response = self._request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            return True, json.dumps(response["error"], sort_keys=True)
        result = response["result"]
        text = "".join(str(block.get("text", "")) for block in result.get("content", []))
        return bool(result.get("isError")), text

    def call_recall(self, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        """``(is_error, text, vector_status)`` of one ``alice_recall`` call.

        A session that traces vectors expects ``arguments`` from ``alice_arm_arguments(..., trace=True)``. It returns the
        text without the trace and the status of the vector stage, and keeps the status in ``vector_calls``. Any
        other session returns the server's text and ``None``.
        """

        is_error, text = self.call(RECALL_TOOL, arguments)
        if is_error or not self.trace_vectors:
            return is_error, text, None
        text, status = split_vector_trace(text)
        self.vector_calls.append(status)
        return False, text, status

    def recall(self, query: str) -> str:
        is_error, text, _status = self.call_recall(alice_arm_arguments(query, trace=self.trace_vectors))
        if is_error:
            raise BenchError("alice_recall returned an error: " + text[:200])
        return text


def alice_arm_arguments(
    query: str,
    *,
    limit: int | None = None,
    context_depth: str | None = None,
    include_sources: bool | None = None,
    trace: bool = False,
    **extra: object,
) -> dict[str, Any]:
    """The arguments the Alice arm sends: the query and nothing else.

    ``limit``, ``context_depth`` and ``include_sources`` stay at the tool's
    defaults. Another value is refused, so the claim can name the setting and an
    answering agent cannot widen its own window.

    ``trace`` is the harness's, not the agent's. It adds ``debug``, which makes the server attach its retrieval
    trace, so the harness can read the status of the vector stage of a vectors run. The session that sent the
    call removes the trace again (``McpSession.call_recall``), so the agent reads the text it would have had
    without it. An agent's own value for ``debug`` is refused with every other extra.
    """

    requested = {"limit": limit, "context_depth": context_depth, "include_sources": include_sources}
    for name, value in requested.items():
        if value is not None and value != PINNED_RECALL_DEFAULTS[name]:
            raise PinnedSettingError(f"{name} is pinned to {PINNED_RECALL_DEFAULTS[name]!r} in the Alice arm")
    if extra:
        raise PinnedSettingError("the Alice arm takes only a query: " + ", ".join(sorted(extra)))
    return {"query": query, "debug": True} if trace else {"query": query}


def recall_settings(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What a set of ``alice_recall`` calls carried, read off the calls themselves.

    ``arguments`` lists each distinct set of arguments sent, without the query (``[{}]``
    when every call sent the query alone). Each of the three pinned settings is the value
    the calls sent, or the tool's own default for a call that left it out: one value when
    every call agrees, the sorted distinct values when they do not.
    """

    if not calls:
        raise BenchError("no alice_recall call was sent, so there are no recall settings to record")
    arguments = sorted(
        {canonical_json({name: value for name, value in call.items() if name != "query"}) for call in calls}
    )
    settings: dict[str, Any] = {"arguments": [json.loads(item) for item in arguments]}
    for name, default in PINNED_RECALL_DEFAULTS.items():
        distinct = sorted({canonical_json(call.get(name, default)) for call in calls})
        settings[name] = json.loads(distinct[0]) if len(distinct) == 1 else [json.loads(item) for item in distinct]
    return settings


def tools_list_digest(tools: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_json(list(tools)).encode("utf-8"))


# --------------------------------------------------------------------------
# Fingerprint


def note_passage_budget(recall: Mapping[str, Any] | None, ranking_mode: str) -> int | None:
    """The note passage budget the recall edge gave its sources stage in this run.

    Recorded beside the pinned settings so a run's claim names the budget it
    used: the caller's limit when one was sent (or the depth is minimal, or
    the mode is document), the tool's default note passage budget otherwise.
    A run that leaves the limit out and runs passage or vectors mode used the
    budget, not the pinned limit of the memory results.
    """

    if recall is None:
        return None
    from alicebot_api.mcp.types import _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET

    if (
        ranking_mode in {"passage", "vectors"}
        and recall["arguments"] == [{}]
        and recall.get("context_depth") != "minimal"
    ):
        return _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET
    limit = recall.get("limit")
    return limit if isinstance(limit, int) else None


def fingerprint(
    *,
    repo: Path,
    gates: Gates,
    session: McpSession | None,
    manifest: Mapping[str, Any] | None,
    search_quality: str | None,
    question_set_sha256: str | None,
    answerer_model: str | None = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """What a number measured. Not ``alicebot_api.__version__``: that reads installed
    distribution metadata and can name a different build than the tree on the path.

    The recall settings are read off the ``alice_recall`` calls the session sent. A session
    that has sent none yet sends one probe call first, so the fields always describe a real call.
    """

    identity = checkout_identity(repo)
    if manifest is not None:
        # The identity is read again here, at the end of a batch as well as at its start, and the
        # vault's build record is held to it. A source file edited while a batch ran would otherwise
        # leave outputs that name two different hashes, one for the build and one for the run.
        build = manifest.get("build")
        if not isinstance(build, dict) or any(key not in build for key in CHECKOUT_IDENTITY_KEYS):
            raise CheckoutError("the manifest does not say which checkout built this vault; build it again with --rebuild")
        moved = differing_identity_keys(build, identity)
        if moved:
            raise CheckoutError(
                "the checkout changed since the vault was built (" + ", ".join(moved) + " differ), "
                "so these outputs would name two checkouts; build again with --rebuild"
            )
    real_file = Path(str(identity["alicebot_api_file"]))
    tools = session.tools_list() if session is not None else None
    recall: dict[str, Any] | None = None
    if session is not None:
        if not session.recall_calls:
            # ``on`` is a harness setting the product recall refuses. The probe
            # uses a value the product accepts. The fingerprint still records ``on``.
            held = os.environ.get(SEARCH_QUALITY_ENV)
            if held == "on":
                os.environ.pop(SEARCH_QUALITY_ENV, None)
            try:
                session.recall(FINGERPRINT_PROBE_QUERY)
            finally:
                if held == "on":
                    os.environ[SEARCH_QUALITY_ENV] = held
        recall = recall_settings(session.recall_calls)
    record: dict[str, Any] = {
        "git": identity["git"],
        "git_sha": identity["git_sha"],
        "dirty": identity["dirty"],
        "checkout_source_sha256": identity["checkout_source_sha256"],
        "alicebot_api_file": str(real_file),
        "python_version": identity["python_version"],
        "sqlite_version": identity["sqlite_version"],
        "alicebot_api_inside_checkout": real_file.is_relative_to(src_dir(repo.resolve()).resolve()),
        "vault_build": None if manifest is None else manifest.get("build"),
        "tools_list_digest": None if tools is None else tools_list_digest(tools),
        "search_quality": search_quality if search_quality is not None else "unset",
        "ranking_mode": resolved_ranking_mode(search_quality),
        "recall_arguments": None if recall is None else recall["arguments"],
        "recall_limit": None if recall is None else recall["limit"],
        "recall_note_passage_budget": note_passage_budget(recall, resolved_ranking_mode(search_quality)),
        "recall_context_depth": None if recall is None else recall["context_depth"],
        "recall_include_sources": None if recall is None else recall["include_sources"],
        "byte_budgets": list(gates.budgets),
        "grep_cap_bytes": gates.grep_cap,
        "corpus_hash": None if manifest is None else manifest.get("corpus_hash"),
        "snapshot_hash": None if manifest is None else manifest.get("snapshot_hash"),
        "question_set_sha256": question_set_sha256,
        "import_order": None if manifest is None else manifest.get("order"),
        "answerer_model": answerer_model,
        "judge_model": judge_model,
        "prompt_hashes": gates.prompt_hashes,
        "gates_sha256": gates.sha256,
        "harness_sha256": identity["harness_sha256"],
    }
    if manifest is not None and "chunk_vectors" in manifest:
        # A vectors run names the model and the digest its vectors came from. Other runs have no such key.
        record["chunk_vectors"] = manifest["chunk_vectors"]
    return record


# --------------------------------------------------------------------------
# The budgeted search command


@dataclass(frozen=True)
class SearchResult:
    text: str
    status: str
    truncated: bool
    raw_bytes: int
    # The status of the vector stage of one recall of a vectors run (``split_vector_trace``), with the seconds the
    # recall took. ``run_budgeted_search`` keeps it in the state folder beside the log. None for every other search.
    vector: Mapping[str, Any] | None = field(default=None, compare=False, hash=False)


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        return 0


def run_budgeted_search(
    state_dir: Path,
    *,
    arm: str,
    search_input: str,
    budget: int,
    runner: Callable[[], SearchResult],
) -> SearchResult:
    """Admit one search, run it and log it, all under one file lock.

    The attempt is counted before it runs, so a search that fails (a bad grep
    pattern, a tool error, any exception) still spends one of the searches and
    still writes its log line, so the count and the log never disagree. A refused
    search writes no log line. The counter and the log live in ``state_dir``, which
    belongs to one answering run, so two processes of one run share one counter.
    """

    count_path = state_dir / "search.count"
    log_path = state_dir / "search_log.jsonl"
    vector_path = state_dir / VECTOR_STATUS_FILENAME
    with _locked(state_dir / "search.lock"):
        used = _read_count(count_path)
        if used >= budget:
            raise BudgetExhausted(f"search budget of {budget} is used")
        tmp = count_path.with_suffix(".tmp")
        tmp.write_text(str(used + 1), encoding="utf-8")
        os.replace(tmp, count_path)
        try:
            result = runner()
        except BenchError as exc:
            result = SearchResult(text=f"error: {exc}", status="error", truncated=False, raw_bytes=0)
        except Exception as exc:  # any failure of a search is logged and counted, never a traceback
            result = SearchResult(
                text=f"error: the search failed ({type(exc).__name__})", status="error", truncated=False, raw_bytes=0
            )
        entry = {
            "n": used + 1,
            "at": utc_now(),
            "arm": arm,
            "input": search_input,
            "status": result.status,
            "bytes": result.raw_bytes,
            "truncated": result.truncated,
            "sha1": hashlib.sha1(result.text.encode("utf-8"), usedforsecurity=False).hexdigest(),
        }
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if result.vector is not None:
            vector_entry = {**result.vector, "n": used + 1, "at": entry["at"], "arm": arm}
            with open(vector_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(vector_entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    return result


def validate_grep_options(options: str) -> list[str]:
    """Split ``options`` and keep only the grep flags an answering agent may use.

    Options that read a file (``-f``), change the file set (``--include``), or
    pipe through another program are refused. ``-r`` is accepted and implied:
    the wrapper searches every file of the snapshot in sorted order, and ``-H``
    is implied, so a one-file snapshot still prints file names as ``grep -r`` does.
    """

    try:
        tokens = shlex.split(options)
    except ValueError as exc:
        raise GrepOptionError("the grep options do not parse") from exc
    accepted: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _GREP_COUNT_OPTIONS:
            index += 1
            if index >= len(tokens) or not tokens[index].isdigit():
                raise GrepOptionError(f"{token} needs a number")
            accepted.extend([token, tokens[index]])
        elif re.fullmatch(r"-[ABCm]\d+", token):
            accepted.append(token)
        elif re.fullmatch(r"-[A-Za-z]+", token) and all(letter in _GREP_LETTERS for letter in token[1:]):
            accepted.append(token)
        else:
            raise GrepOptionError(f"grep option {token!r} is not allowed")
        index += 1
    return accepted


def cut_to_cap(data: bytes, cap: int | None) -> tuple[str, bool]:
    """The grep output an agent sees: cut at ``cap`` bytes, with the cut stated."""

    if cap is None or len(data) <= cap:
        return data.decode("utf-8", errors="replace"), False
    cut = data[:cap].decode("utf-8", errors="ignore")
    return cut + f"\n[output cut at {cap} bytes]\n", True


def grep_search(snapshot_dir: Path, *, pattern: str, options: str, cap: int | None) -> SearchResult:
    grep = shutil.which("grep")
    if grep is None:
        raise BenchError("grep is not installed")
    flags = validate_grep_options(options)
    files = sorted(
        path.relative_to(snapshot_dir).as_posix() for path in snapshot_dir.rglob("*") if path.is_file()
    )
    if not files:
        raise BenchError("the snapshot holds no files")
    env = {name: value for name, value in os.environ.items() if name not in {"GREP_OPTIONS", "GREP_COLOR", "GREP_COLORS"}}
    try:
        completed = subprocess.run(
            [grep, "-H", *flags, "-e", pattern, "--", *files],
            cwd=snapshot_dir,
            capture_output=True,
            check=False,
            env=env,
            timeout=GREP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchError("grep timed out") from exc
    if completed.returncode not in (0, 1):
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BenchError("grep failed: " + message[:200])
    text, truncated = cut_to_cap(completed.stdout, cap)
    return SearchResult(text=text, status="ok", truncated=truncated, raw_bytes=len(completed.stdout))


# --------------------------------------------------------------------------
# Command line


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, help="Directory this harness owns. Vault, snapshot and manifest live here.")
    parser.add_argument("--data-dir", default=None, help="Vault directory. Must be inside the run directory.")
    parser.add_argument("--checkout", default=None, help="Repository whose apps/api/src is put first on the path.")
    parser.add_argument("--gates", default=None, help="Path of gates.json. Defaults to the one in the repository.")
    parser.add_argument(
        "--search-quality",
        default=None,
        choices=SEARCH_QUALITY_CHOICES,
        help="Set ALICE_SEARCH_QUALITY for the run. Unset by default. vectors needs a build made with --embeddings on.",
    )


def build_parser(commands: dict[str, Callable[[argparse.Namespace], int]] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alice_bench", description=__doc__.split("\n\n", 1)[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Import a corpus folder into a fresh vault.")
    _add_run_arguments(build)
    build.add_argument("--corpus", required=True, help="Folder of Markdown files.")
    build.add_argument("--order", default="sorted", help="sorted, reverse or shuffle:SEED.")
    build.add_argument("--domain", default="project")
    build.add_argument("--sensitivity", default="internal")
    build.add_argument("--questions", default=None, help="Question set whose anchors are checked after the build.")
    build.add_argument("--rebuild", action="store_true", help="Replace an earlier build in the run directory.")
    build.add_argument("--strict-flags", action="store_true", help="Fail when an anchor is flagged and not approved.")
    build.add_argument(
        "--embeddings",
        default="off",
        choices=("off", "on"),
        help="With --search-quality vectors: embed the chunks through the product at a loopback endpoint (required).",
    )
    build.add_argument("--endpoint", default=DEFAULT_OLLAMA_BASE, help="Loopback Ollama address, for --embeddings on.")
    build.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="Embedding model, for --embeddings on.")

    anchors = sub.add_parser("anchors", help="Check anchors against the snapshot of a built vault.")
    _add_run_arguments(anchors)
    anchors.add_argument("--questions", required=True)
    anchors.add_argument("--strict-flags", action="store_true")

    recall = sub.add_parser("recall", help="Print the recall text for one query.")
    _add_run_arguments(recall)
    recall.add_argument("--query", required=True)

    batch = sub.add_parser("batch", help="Run a question set, both variants, and save the outputs.")
    _add_run_arguments(batch)
    batch.add_argument("--questions", required=True)
    batch.add_argument("--out", required=True)
    batch.add_argument("--answerer-model", default=None)
    batch.add_argument("--judge-model", default=None)

    search = sub.add_parser("search", help="The budgeted command an answering agent runs.")
    _add_run_arguments(search)
    search.add_argument("--arm", required=True, choices=("alice", "grep", "grep-uncapped"))
    search.add_argument("--state-dir", default=None, help="Counter and log of one answering run. Inside the run directory.")
    search.add_argument("--budget", type=int, default=None, help="Searches allowed. Defaults to gates.json.")
    search.add_argument("--query", default=None, help="Alice arm: the query.")
    search.add_argument("--pattern", default=None, help="Grep arms: the pattern.")
    search.add_argument(
        "--grep-options",
        default="",
        help="Grep arms: a few flags, written with an equals sign because they start with a dash, as in --grep-options='-i -n -C 2'.",
    )
    search.add_argument("--limit", type=int, default=None, help="Refused unless it equals the default.")
    search.add_argument("--context-depth", default=None, help="Refused unless it equals the default.")
    search.add_argument("--include-sources", default=None, help="Refused unless it equals the default.")

    score = sub.add_parser("score", help="Tier 1 over saved outputs.")
    score.add_argument("--questions", required=True)
    score.add_argument("--outputs", required=True, nargs="+", help="One outputs file per import order.")
    score.add_argument("--gates", default=None)
    score.add_argument("--json", action="store_true", help="Print the report as JSON.")
    score.add_argument("--per-question", action="store_true", help="Print a line per question. Dev sets only.")

    finger = sub.add_parser("fingerprint", help="Print the fingerprint of a run.")
    _add_run_arguments(finger)
    finger.add_argument("--questions", default=None)
    # ``python scripts/alice_bench.py`` puts the scripts directory on the path, not the
    # repository root, so the showcase module would not import. The handlers must be
    # registered on this module's command table: importing the module again under
    # ``scripts.alice_bench`` is a different module when this file is the program.
    repo = str(REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from scripts.showcase_bench import add_showcase_commands

    add_showcase_commands(sub, _COMMANDS if commands is None else commands)
    return parser


def _context(args: argparse.Namespace, *, create: bool = False) -> tuple[Path, Path, Path, Gates]:
    run_dir = prepare_run_dir(Path(args.run_dir), create=True) if create else require_run_dir(Path(args.run_dir))
    data_dir = check_vault_location(run_dir, run_dir / VAULT_DIRNAME if args.data_dir is None else Path(args.data_dir))
    repo = Path(args.checkout) if args.checkout else REPO_ROOT
    gates = load_gates(Path(args.gates) if args.gates else None)
    activate_checkout(repo)
    if not create:
        manifest = require_matching_build(_read_manifest(run_dir), repo)
        require_matching_vault(manifest, run_dir, data_dir)
        require_quality_of_build(manifest, getattr(args, "search_quality", None))
        if getattr(args, "search_quality", None) == VECTORS and getattr(args, "arm", "alice") == "alice":
            settings = manifest.get("embedding_env")
            if not isinstance(settings, dict):
                raise BenchError("this vectors run recorded no embedding settings; build it again with --embeddings on")
            require_vector_readiness(
                vault_path(data_dir),
                manifest.get("chunk_vectors"),
                settings,
                cache_path=run_dir / VECTOR_CENSUS_FILENAME,
            )
    return run_dir, data_dir, repo, gates


def require_quality_of_build(manifest: Mapping[str, Any], asked: str | None) -> None:
    """Refuse a vectors command on a vault built another way, and a command of another quality on a vault built for vectors.

    The other qualities may be asked of one vault in turn, as they always could. ``vectors`` may not: a vault built
    without vectors has none to rank by, and a vault built with them is read by a run that carries the opt-in and the
    endpoint, which only a command of that quality does.
    """

    built = str(manifest["build"].get("search_quality", "unset"))
    wanted = "unset" if asked is None else asked
    if VECTORS in (built, wanted) and built != wanted:
        raise BenchError(f"this vault was built for search quality {built} and the command asks for {wanted}")


def vector_settings_of(args: argparse.Namespace) -> dict[str, str] | None:
    """The embedding settings of a command of search quality ``vectors``, and None for any other command or arm.

    A command that builds (it has ``--embeddings``) takes them from ``--endpoint`` and ``--embed-model`` and
    refuses to go on without ``--embeddings on``. A command that reads a run takes the ones its build recorded.
    """

    if getattr(args, "search_quality", None) != VECTORS:
        return None
    if getattr(args, "arm", "alice") != "alice":
        # An arm that reads no vector (a grep, a note folder, a keyword stand-in) carries the quality and no endpoint.
        return None
    embeddings = getattr(args, "embeddings", None)
    if embeddings is not None:
        if embeddings != "on":
            raise BenchError("--search-quality vectors needs --embeddings on")
        return embedding_settings(args.endpoint, args.embed_model)
    run_dir = require_run_dir(Path(args.run_dir))
    name = "showcase.json" if str(args.command).startswith("showcase-") else MANIFEST_FILENAME
    try:
        record = json.loads((run_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError("a vectors run needs the record of its build in the run directory") from exc
    settings = record.get("embedding_env") if isinstance(record, dict) else None
    if not isinstance(settings, dict):
        raise BenchError(
            "this run recorded no embedding settings, so it was not built with --search-quality vectors --embeddings on"
        )
    return {str(key): str(value) for key, value in settings.items()}


def command_environment(args: argparse.Namespace) -> dict[str, str]:
    """The variables a command may carry: its search quality and, for ``vectors``, its embedding variables."""

    return arm_environment(getattr(args, "search_quality", None), vector_settings=vector_settings_of(args))


def _read_manifest(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / MANIFEST_FILENAME
    if not path.is_file():
        return None
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _print_anchor_report(report: Mapping[str, Any], *, strict: bool) -> int:
    print(
        f"anchors: {report['anchors']} verbatim, {report['flagged']} flagged, "
        f"{report['flagged_unapproved']} flagged and not approved"
    )
    for row in report["rows"]:
        if row["flags"] and not row["approved"]:
            print(
                f"  flagged {row['question']} fact {row['fact']}: {','.join(row['flags'])} "
                f"(length {row['length']}, files {row['files']})"
            )
    if strict and report["flagged_unapproved"]:
        return EXIT_REFUSED
    return EXIT_OK


def _cmd_build(args: argparse.Namespace) -> int:
    if args.embeddings == "on" and args.search_quality != VECTORS:
        raise BenchError("--embeddings on is for --search-quality vectors")
    run_dir, data_dir, repo, gates = _context(args, create=True)
    manifest = build_vault(
        corpus_dir=Path(args.corpus),
        run_dir=run_dir,
        data_dir=data_dir,
        order=args.order,
        domain=args.domain,
        sensitivity=args.sensitivity,
        rebuild=args.rebuild,
        repo=repo,
        search_quality=args.search_quality,
        vector_settings=vector_settings_of(args),
    )
    print(
        f"built {manifest['sources']} sources, {manifest['chunks']} chunks, order {manifest['order']}, "
        f"{len(manifest['withheld'])} files with withheld lines"
    )
    if "chunk_vectors" in manifest:
        print(format_chunk_vectors(manifest["chunk_vectors"]))
    if args.questions:
        qset = load_questions(Path(args.questions))
        report = verify_anchors(qset, read_snapshot(run_dir / SNAPSHOT_DIRNAME), gates)
        return _print_anchor_report(report, strict=args.strict_flags)
    return EXIT_OK


def _cmd_anchors(args: argparse.Namespace) -> int:
    run_dir, _data_dir, _repo, gates = _context(args)
    qset = load_questions(Path(args.questions))
    report = verify_anchors(qset, read_snapshot(run_dir / SNAPSHOT_DIRNAME), gates)
    return _print_anchor_report(report, strict=args.strict_flags)


def _cmd_recall(args: argparse.Namespace) -> int:
    _run_dir, data_dir, _repo, _gates = _context(args)
    session = McpSession(vault_path(data_dir), trace_vectors=args.search_quality == VECTORS)
    sys.stdout.write(session.recall(args.query))
    sys.stdout.write("\n")
    for status in session.vector_calls:
        sys.stderr.write("vector stage: " + str(status["status"]) + "\n")
    return EXIT_OK


def _cmd_batch(args: argparse.Namespace) -> int:
    run_dir, data_dir, repo, gates = _context(args)
    qset = load_questions(Path(args.questions))
    db_path = vault_path(data_dir)
    session = McpSession(db_path, trace_vectors=args.search_quality == VECTORS)
    before = vault_row_counts(db_path)
    outputs: dict[str, dict[str, str]] = {}
    for question in qset.questions:
        outputs[question.id] = {variant: session.recall(question.query_for(variant)) for variant in VARIANTS}
    after = vault_row_counts(db_path)
    if before != after:
        raise BenchError("a recall changed rows in the vault: " + ", ".join(sorted(k for k in before if before[k] != after.get(k))))
    document = {
        "schema": OUTPUTS_SCHEMA,
        "question_set_sha256": qset.sha256,
        "fingerprint": fingerprint(
            repo=repo,
            gates=gates,
            session=session,
            manifest=_read_manifest(run_dir),
            search_quality=args.search_quality,
            question_set_sha256=qset.sha256,
            answerer_model=args.answerer_model,
            judge_model=args.judge_model,
        ),
        "outputs": outputs,
    }
    if args.search_quality == VECTORS:
        document["vector_stage"] = vector_stage_summary(session.vector_calls)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(outputs)} questions, {len(VARIANTS)} variants, to {out_path.name}")
    return EXIT_OK


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    run_dir, data_dir, repo, gates = _context(args)
    qsha = load_questions(Path(args.questions)).sha256 if args.questions else None
    print(
        json.dumps(
            fingerprint(
                repo=repo,
                gates=gates,
                session=McpSession(vault_path(data_dir), trace_vectors=args.search_quality == VECTORS),
                manifest=_read_manifest(run_dir),
                search_quality=args.search_quality,
                question_set_sha256=qsha,
            ),
            indent=1,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _parse_bool_option(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise PinnedSettingError("--include-sources takes true or false")


def _cmd_search(args: argparse.Namespace) -> int:
    run_dir, data_dir, _repo, gates = _context(args)
    state_dir = resolve_inside(run_dir, args.state_dir, default=run_dir / STATE_DIRNAME / "default", what="--state-dir")
    budget = gates.searches_per_run if args.budget is None else args.budget
    if args.arm == "alice":
        if args.query is None:
            raise BenchError("the alice arm needs --query")
        vectors = args.search_quality == VECTORS
        arguments = alice_arm_arguments(
            args.query,
            limit=args.limit,
            context_depth=args.context_depth,
            include_sources=_parse_bool_option(args.include_sources),
            trace=vectors,
        )
        session = McpSession(vault_path(data_dir), trace_vectors=vectors)

        def runner() -> SearchResult:
            started = time.monotonic()
            is_error, text, status = session.call_recall(arguments)
            vector = None if status is None else {**status, "seconds": round(time.monotonic() - started, 3)}
            return SearchResult(
                text=text,
                status="error" if is_error else "ok",
                truncated=False,
                raw_bytes=len(text.encode("utf-8")),
                vector=vector,
            )

        search_input = args.query
    else:
        if args.pattern is None:
            raise BenchError("the grep arms need --pattern")
        cap = gates.grep_cap if args.arm == "grep" else None
        snapshot_dir = run_dir / SNAPSHOT_DIRNAME
        pattern, options = args.pattern, args.grep_options
        validate_grep_options(options)

        def runner() -> SearchResult:
            return grep_search(snapshot_dir, pattern=pattern, options=options, cap=cap)

        search_input = json.dumps({"pattern": pattern, "options": options}, sort_keys=True)
    result = run_budgeted_search(state_dir, arm=args.arm, search_input=search_input, budget=budget, runner=runner)
    sys.stdout.write(result.text)
    if not result.text.endswith("\n"):
        sys.stdout.write("\n")
    return EXIT_OK if result.status == "ok" else EXIT_SEARCH_ERROR


def _cmd_score(args: argparse.Namespace) -> int:
    gates = load_gates(Path(args.gates) if args.gates else None)
    qset = load_questions(Path(args.questions))
    documents: list[tuple[str, dict[str, Any]]] = []
    for path_text in args.outputs:
        path = Path(path_text)
        documents.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    require_comparable_outputs(documents)
    reports: list[tuple[str, dict[str, Any]]] = []
    for name, document in documents:
        fp = document.get("fingerprint") or {}
        label = str(fp.get("import_order") or Path(name).stem)
        reports.append((label, score_outputs_document(document, qset, gates)))
    minimums = minimum_across_orders(reports, gates)
    if args.json:
        print(
            json.dumps(
                {
                    "orders": {
                        label: (report if args.per_question else without_per_question(report))
                        for label, report in reports
                    },
                    "minimum_hits": minimums,
                    "gates_sha256": gates.sha256,
                },
                indent=1,
                sort_keys=True,
            )
        )
        return EXIT_OK
    for label, report in reports:
        print(format_report(f"order {label}", report, gates, per_question=args.per_question))
        print()
    if len(reports) > 1:
        print("minimum hits over the import orders")
        for variant, by_budget in minimums.items():
            print("  " + variant + ": " + ", ".join(f"{budget} bytes {value}" for budget, value in by_budget.items()))
    print(f"gates.json sha256 {gates.sha256}")
    return EXIT_OK


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "build": _cmd_build,
    "anchors": _cmd_anchors,
    "recall": _cmd_recall,
    "batch": _cmd_batch,
    "search": _cmd_search,
    "score": _cmd_score,
    "fingerprint": _cmd_fingerprint,
}


def _refusal_types() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """Bench errors of this module and of ``scripts.alice_bench`` when the file is the program.

    Running ``python scripts/alice_bench.py`` loads this file as ``__main__``. The showcase
    commands import it again as ``scripts.alice_bench``, so their exceptions are a second class.
    """

    budgets: list[type[BaseException]] = [BudgetExhausted]
    errors: list[type[BaseException]] = [BenchError]
    other = sys.modules.get("scripts.alice_bench")
    if other is not None and other is not sys.modules.get(__name__):
        budgets.append(other.BudgetExhausted)
        errors.append(other.BenchError)
    return tuple(budgets), tuple(errors)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    budget_types, error_types = _refusal_types()
    try:
        with scoped_environment(command_environment(args)):
            code = _COMMANDS[args.command](args)
            require_harness_unchanged()
            return code
    except budget_types as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except error_types as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
