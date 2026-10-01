"""LongMemEval run orchestration: worker pool, checkpoints, and reports.

Entry point is ``scripts/run_longmemeval.py`` (repo root), which puts
``eval/`` on ``sys.path`` and calls :func:`main`. Long runs checkpoint one
JSONL record per question so ``--resume`` can pick up where a run stopped;
the final report aggregates overall and per-question-type accuracy plus
retrieval statistics and a config fingerprint.

``--dry-run`` ingests and retrieves only (no chat model, no judge) and fails
if retrieval comes back empty for a non-abstention question. It skips
cleanly (exit 0) when the dataset has not been fetched, so it is safe in CI.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

_EVAL_DIR = Path(__file__).resolve().parent.parent
if str(_EVAL_DIR) not in sys.path:  # direct execution: python eval/longmemeval/runner.py
    sys.path.insert(0, str(_EVAL_DIR))

from alicebot_api import __version__ as alicebot_version
from alicebot_api.vnext_embeddings import EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV
from alicebot_api.vnext_reranker import (
    RERANKER_BASE_URL_ENV,
    RERANKER_MODEL_ENV,
    RERANK_PROMPT_SHA256,
)

from longmemeval.adapter import (
    ANSWER_MAX_TOKENS,
    ANSWER_MAX_TOKENS_COT,
    DEFAULT_CONTEXT_CHAR_BUDGET,
    DEFAULT_EXCERPT_SOURCE,
    DEFAULT_MAX_ITEMS,
    DEFAULT_PROMOTION_MODE,
    DEFAULT_SURFACE,
    EXCERPT_SOURCE_PACK_EXCERPTS,
    EXCERPT_SOURCES,
    PROMOTION_MODES,
    PROMOTION_MODE_SOURCES_ONLY,
    RECALL_MAX_LIMIT,
    RECALL_RESULT_FORMAT,
    SURFACES,
    SURFACE_RECALL,
    build_answer_prompt,
    context_char_budget_from_env,
    max_items_from_env,
    question_run,
    recall_surface_blocker,
    resolve_excerpt_source,
    validate_promotion_mode,
    validate_surface,
)
from longmemeval.chat import (
    ChatModelConfig,
    chat_completion,
    judge_config_from_env,
    model_config_from_env,
    redacted_base_url,
)
from longmemeval.dataset import (
    RESULTS_DIR,
    VARIANTS,
    WORK_DIR,
    LongMemEvalDatasetError,
    LongMemEvalQuestion,
    load_dataset,
    resolve_dataset_path,
)
from longmemeval.judge import judge_hypothesis
from longmemeval.pack_formats import DEFAULT_PACK_FORMAT, PACK_FORMATS
from longmemeval.session_labels import (
    DEFAULT_SESSION_LABEL_MODE,
    SESSION_LABEL_MODE_RAW,
    SessionLabelError,
    SessionLabelSidecar,
    key_id_for_mode,
    sidecar_path_for,
    validate_dataset_labels,
    validate_session_label_mode,
)
from longmemeval.verification import (
    apply_grounding_gate,
    make_chat_client,
    verifier_config_from_env,
    verify_grounding,
)


RESULT_SCHEMA = "longmemeval_result_v1"
REPORT_SCHEMA = "longmemeval_report_v1"
# 1.1: session labels (keyed hash by default; "raw" reproduces 1.0 runs), and
# excerpt source, promotion mode and surface recorded in the fingerprint and on
# every row. A 1.1 run never shares a fingerprint digest with a 1.0 run.
HARNESS_VERSION = "1.1"
INGEST_MARKER_SCHEMA = "longmemeval_ingest_marker_v2"
GENERATION_TEMPERATURE = 0.0

EXIT_OK = 0
EXIT_RUN_FAILURES = 1
EXIT_CONFIG_ERROR = 2

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Every source file executed while constructing a reusable LongMemEval store.
# Keep this manifest explicit: a code change in any capture, promotion, or
# optional roll-up path must invalidate an existing ``*.ingested.json`` marker.
_INGEST_CODE_MANIFEST = (
    Path("eval/longmemeval/adapter.py"),
    Path("eval/longmemeval/session_labels.py"),
    Path("apps/api/src/alicebot_api/sqlite_store.py"),
    Path("apps/api/src/alicebot_api/vnext_capture.py"),
    Path("apps/api/src/alicebot_api/vnext_embeddings.py"),
    Path("apps/api/src/alicebot_api/vnext_entities.py"),
    Path("apps/api/src/alicebot_api/vnext_fact_keys.py"),
    Path("apps/api/src/alicebot_api/vnext_memory_commit.py"),
    Path("apps/api/src/alicebot_api/vnext_rollups.py"),
)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    variant: str
    dataset_path: Path
    limit: int | None
    question_ids: tuple[str, ...] | None
    question_ids_file: str | None
    resume: bool
    dry_run: bool
    cot: bool
    workers: int
    max_items: int
    context_char_budget: int
    work_dir: Path
    checkpoint_path: Path
    report_path: Path
    keep_stores: bool
    # Disclosed post-generation grounding gate (see longmemeval/verification.py);
    # off by default and always visible in the config fingerprint.
    verify_grounding: bool = False
    # Disclosed post-ingest consolidation step (see adapter.py,
    # QuestionRun._consolidate_and_accept_rollups): propose roll-up cards via
    # the product's consolidation pass and review-accept them through the
    # real acceptance path. Off by default so the default replay stays
    # byte-identical to published runs; always in the config fingerprint.
    accept_rollups: bool = False
    # ---- pack-format (structured JSON packs) integration begin ----------
    # How the retrieved context is rendered into the reading template's
    # history slot: "prose" (default, byte-identical to published runs) or
    # "json" (the same content as a compact structured document — the
    # LongMemEval authors' best-performing reading format). Always in the
    # config fingerprint so no run can hide its format.
    pack_format: str = DEFAULT_PACK_FORMAT
    # ---- pack-format (structured JSON packs) integration end ------------
    # Reuse per-question stores previously ingested by the coverage probe or
    # a --keep-stores run (probe-style ``*.ingested.json`` markers, dataset
    # hash checked). Skips session capture only; promotion and roll-up
    # acceptance still run (both idempotent). Always in the fingerprint and
    # per-row ``ingest.reused_store`` so reuse can never be silent.
    reuse_stores: bool = False
    # ---- the four run-level choices that decide what a score measures -------
    # Each lands in the fingerprint and on every checkpoint row, and resume
    # refuses to mix rows that differ in any of them. Defaults: anonymised
    # session labels (raw is the opt-in for reproducing 1.0 runs), the
    # privileged chunk reader, force-accepted memories and the context pack,
    # which is what every published run used apart from the labels.
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE
    excerpt_source: str = DEFAULT_EXCERPT_SOURCE
    promotion_mode: str = DEFAULT_PROMOTION_MODE
    surface: str = DEFAULT_SURFACE

    @property
    def mode(self) -> str:
        return "dry_run" if self.dry_run else "scored"

    @property
    def effective_pack_format(self) -> str:
        """What the reader's context is rendered as; the recall tool's result is its own format."""
        return RECALL_RESULT_FORMAT if self.surface == SURFACE_RECALL else self.pack_format

    def run_choices(self) -> dict[str, object]:
        """The per-row copy of the run-level choices (also in the fingerprint)."""
        return {
            "harness_version": HARNESS_VERSION,
            "session_label_mode": self.session_label_mode,
            "session_label_key_id": key_id_for_mode(self.session_label_mode),
            "excerpt_source": self.excerpt_source,
            "promotion_mode": self.promotion_mode,
            "surface": self.surface,
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_prefix(path: Path, *, length: int = 16) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:length]


def _untracked_source_identity(repo_root: Path, untracked_output: bytes) -> dict[str, object]:
    untracked_paths = sorted(path for path in untracked_output.split(b"\0") if path)
    untracked_digest = hashlib.sha256()
    root_absolute = repo_root.absolute()
    for encoded_path in untracked_paths:
        relative_path = encoded_path.decode("utf-8", errors="surrogateescape")
        path = (repo_root / relative_path).absolute()
        if not path.is_relative_to(root_absolute):
            raise RuntimeError("untracked benchmark source entry escaped the repository")
        untracked_digest.update(encoded_path)
        untracked_digest.update(b"\0")
        try:
            if path.is_symlink():
                untracked_digest.update(b"symlink\0")
                untracked_digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            else:
                untracked_digest.update(path.read_bytes())
        except OSError as exc:
            raise RuntimeError("cannot fingerprint an untracked benchmark source entry") from exc
        untracked_digest.update(b"\0")
    return {
        "untracked_tree_dirty": bool(untracked_paths),
        "untracked_file_count": len(untracked_paths),
        "untracked_manifest_sha256_prefix": (
            untracked_digest.hexdigest()[:16] if untracked_paths else None
        ),
    }


def _source_provenance() -> dict[str, object]:
    """Return reviewable source identity without ever serializing the diff.

    Benchmark evidence is only attributable when the exact source tree is
    recorded.  A dirty tree is supported for development runs, but its
    tracked diff and every non-ignored untracked file are represented by
    digests so local source or secrets never enter a checkpoint. Ignored files
    (including ``.env``) are deliberately outside this source identity.
    """
    repo_root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        untracked_output = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot fingerprint benchmark source tree") from exc

    return {
        "git_commit": commit or None,
        "tracked_tree_dirty": bool(diff),
        "tracked_diff_sha256_prefix": hashlib.sha256(diff).hexdigest()[:16] if diff else None,
        **_untracked_source_identity(repo_root, untracked_output),
    }


def _question_ingest_digest(question: LongMemEvalQuestion) -> str:
    payload = {
        "question_id": question.question_id,
        "sessions": [
            {
                "session_id": session_id,
                "date": date,
                "turns": [
                    {"role": turn.role, "content": turn.content, "has_answer": turn.has_answer}
                    for turn in turns
                ],
            }
            for session_id, date, turns in question.sessions_with_metadata()
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _ingest_code_digest(*, repo_root: Path | None = None) -> str:
    """Digest the production ingestion implementation used by store reuse."""
    resolved_root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative_path in _INGEST_CODE_MANIFEST:
        path = resolved_root / relative_path
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _build_ingest_marker_payload(
    question: LongMemEvalQuestion,
    *,
    dataset_path: Path,
    accept_rollups: bool,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
    promotion_mode: str = DEFAULT_PROMOTION_MODE,
) -> dict[str, object]:
    embeddings_base_url = os.environ.get(EMBEDDINGS_BASE_URL_ENV, "").strip()
    embeddings_model = os.environ.get(EMBEDDINGS_MODEL_ENV, "").strip()
    return {
        "schema": INGEST_MARKER_SCHEMA,
        "question_id": question.question_id,
        "session_count": len(question.haystack_session_ids),
        "dataset_sha256_prefix": _sha256_prefix(dataset_path),
        "question_ingest_sha256_prefix": _question_ingest_digest(question),
        "ingest_code_sha256_prefix": _ingest_code_digest(),
        "alicebot_version": alicebot_version,
        "embeddings_enabled": bool(embeddings_base_url and embeddings_model),
        "embeddings_model": embeddings_model or None,
        "embeddings_base_url": redacted_base_url(embeddings_base_url) if embeddings_base_url else None,
        "accept_rollups": accept_rollups,
        # A store ingested under one label mode or promotion mode is a
        # different store: raw ids sit in its text and metadata, or its
        # memories were accepted. Reusing it under another mode would mix them.
        "session_label_mode": session_label_mode,
        "session_label_key_id": key_id_for_mode(session_label_mode),
        "promotion_mode": promotion_mode,
    }


def _ingest_marker_payload(question: LongMemEvalQuestion, config: RunnerConfig) -> dict[str, object]:
    return _build_ingest_marker_payload(
        question,
        dataset_path=config.dataset_path,
        accept_rollups=config.accept_rollups,
        session_label_mode=config.session_label_mode,
        promotion_mode=config.promotion_mode,
    )


def config_fingerprint(
    config: RunnerConfig,
    *,
    model: ChatModelConfig | None,
    judge: ChatModelConfig | None,
    verifier: ChatModelConfig | None = None,
) -> dict[str, object]:
    """Everything needed to interpret a score; digest detects config drift."""
    embeddings_base_url = os.environ.get(EMBEDDINGS_BASE_URL_ENV, "").strip()
    embeddings_model = os.environ.get(EMBEDDINGS_MODEL_ENV, "").strip()
    reranker_base_url = os.environ.get(RERANKER_BASE_URL_ENV, "").strip()
    reranker_model = os.environ.get(RERANKER_MODEL_ENV, "").strip()
    fingerprint: dict[str, object] = {
        "harness_version": HARNESS_VERSION,
        "alicebot_version": alicebot_version,
        "mode": config.mode,
        "variant": config.variant,
        "dataset_file": config.dataset_path.name,
        "dataset_sha256_prefix": _sha256_prefix(config.dataset_path),
        "answer_model": model.redacted() if model is not None else None,
        "judge_model": judge.redacted() if judge is not None else None,
        "embeddings_enabled": bool(embeddings_base_url and embeddings_model),
        "embeddings_base_url": redacted_base_url(embeddings_base_url) if embeddings_base_url else None,
        "embeddings_model": embeddings_model or None,
        "reranker_enabled": bool(reranker_base_url and reranker_model),
        "reranker_base_url": redacted_base_url(reranker_base_url) if reranker_base_url else None,
        "reranker_model": reranker_model or None,
        "reranker_prompt_sha256": RERANK_PROMPT_SHA256 if reranker_base_url and reranker_model else None,
        "source": _source_provenance(),
        "reading_style": "cot" if config.cot else "standard",
        # The grounding gate can never run undisclosed: the flag (and the
        # verifier model when enabled) always feed the fingerprint digest.
        "verify_grounding": config.verify_grounding,
        "verifier_model": verifier.redacted() if config.verify_grounding and verifier is not None else None,
        # Roll-up acceptance can never run undisclosed either: the flag feeds
        # the digest, and per-store proposed/accepted counts land in each
        # checkpoint row's ingest.rollups block.
        "accept_rollups": config.accept_rollups,
        # Pack format (prose | json) always feeds the digest: a JSON-pack
        # run can never masquerade as a prose run or vice versa. A recall
        # run records the tool-result format instead (the reader sees the
        # text the MCP server returns, not a rendered pack).
        "pack_format": config.effective_pack_format,
        # The four run-level choices (harness 1.1). Before 1.1 none of these
        # was recorded: a run that read store chunks and one that read pack
        # excerpts had the same digest, and the raw dataset session ids were
        # shown to the reader without saying so.
        "session_label_mode": config.session_label_mode,
        "session_label_key_id": key_id_for_mode(config.session_label_mode),
        "excerpt_source": config.excerpt_source,
        "promotion_mode": config.promotion_mode,
        "surface": config.surface,
        # Store reuse always feeds the digest too (and lands per-row as
        # ingest.reused_store), so ingest reuse can never be silent.
        "reuse_stores": config.reuse_stores,
        "generation_temperature": GENERATION_TEMPERATURE,
        "max_items": config.max_items,
        # The recall surface hands the reader the tool's result as returned,
        # so no character budget applies and none is claimed.
        "context_char_budget": None if config.surface == SURFACE_RECALL else config.context_char_budget,
        # A slice run must never masquerade as a full run: the subset (file
        # name, count, digest of the sorted ids) feeds the fingerprint digest.
        "question_subset": None
        if config.question_ids is None
        else {
            "file": config.question_ids_file,
            "count": len(config.question_ids),
            "ids_sha256_prefix": hashlib.sha256(
                "\n".join(sorted(config.question_ids)).encode("utf-8")
            ).hexdigest()[:16],
        },
    }
    digest_source = json.dumps(fingerprint, sort_keys=True, ensure_ascii=True)
    fingerprint["digest"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16]
    return fingerprint


def load_question_ids(path: Path) -> tuple[str, ...]:
    """Read a slice file: one question_id per line.

    Blank lines and ``#`` comment lines are skipped; duplicates keep the
    first occurrence. Raises ``ValueError`` for a missing or empty file.
    """
    if not path.is_file():
        raise ValueError(f"question-ids file does not exist: {path}")
    ids: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in seen:
            seen.add(stripped)
            ids.append(stripped)
    if not ids:
        raise ValueError(f"question-ids file contains no question ids: {path}")
    return tuple(ids)


# -- checkpointing -----------------------------------------------------------


def load_checkpoint(path: Path) -> dict[str, dict[str, object]]:
    """Read a checkpoint JSONL into ``question_id -> record``, last one wins."""
    records: dict[str, dict[str, object]] = {}
    if not path.is_file():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"[runner] skipping corrupt checkpoint line {line_number} in {path}", file=sys.stderr)
                continue
            if isinstance(record, dict) and isinstance(record.get("question_id"), str):
                records[record["question_id"]] = record
    return records


def completed_question_ids(records: dict[str, dict[str, object]], *, mode: str) -> set[str]:
    """Question ids that do not need re-running for this mode."""
    return {
        question_id
        for question_id, record in records.items()
        if record.get("status") == "ok" and record.get("mode") == mode
    }


_MISSING = object()


def resume_conflicts(
    records: dict[str, dict[str, object]],
    done_ids: set[str],
    *,
    config: RunnerConfig,
    fingerprint_digest: str,
) -> list[str]:
    """Why completed rows cannot be mixed with this run; empty when they can.

    The fingerprint digest already covers every run-level choice, so any
    difference trips it. The per-row fields are checked on their own as well,
    for two reasons: a refusal can then name the field that differs, and a row
    written by code that predates a field (it has no value at all) still
    cannot slide in under a matching digest.
    """
    expected = config.run_choices()
    digest_mismatches = sorted(
        question_id for question_id in done_ids if records[question_id].get("fingerprint_digest") != fingerprint_digest
    )
    conflicts: list[str] = []
    if digest_mismatches:
        conflicts.append(f"{len(digest_mismatches)} completed rows were produced with a different config fingerprint")
    for key, wanted in expected.items():
        differing = sorted(
            question_id for question_id in done_ids if records[question_id].get(key, _MISSING) != wanted
        )
        if differing:
            found = sorted({repr(records[question_id].get(key, "<missing>")) for question_id in differing})
            conflicts.append(
                f"{len(differing)} completed rows differ in {key} (rows: {', '.join(found)}; this run: {wanted!r})"
            )
    return conflicts


class CheckpointWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=True, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


# -- per-question pipeline ----------------------------------------------------


def _db_path_for(config: RunnerConfig, question_id: str) -> Path:
    return config.work_dir / f"{_FILENAME_SAFE.sub('_', question_id)}.sqlite3"


def _cleanup_store(db_path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _reuse_marker_matches(marker_path: Path, question: LongMemEvalQuestion, *, config: RunnerConfig) -> bool:
    """Probe-compatible ingest-marker check for --reuse-stores.

    The marker was written only after a clean, committed ingest (by the
    coverage probe or an earlier --keep-stores run); the dataset hash guards
    against reusing stores built from a different dataset revision.
    """
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker == _ingest_marker_payload(question, config)


def run_question(
    question: LongMemEvalQuestion,
    config: RunnerConfig,
    *,
    model: ChatModelConfig | None,
    judge: ChatModelConfig | None,
    fingerprint_digest: str,
    verifier: ChatModelConfig | None = None,
    label_sidecar: SessionLabelSidecar | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "mode": config.mode,
        "question_id": question.question_id,
        "question_type": question.question_type,
        "is_abstention": question.is_abstention,
        "gold_answer": question.answer,
        "fingerprint_digest": fingerprint_digest,
        # The run-level choices ride on every row, error rows included, so a
        # row can be read (and a resume checked) without the report file.
        **config.run_choices(),
        "status": "ok",
        "error": None,
        "hypothesis": None,
        "judge": None,
        "generation": None,
    }
    db_path = _db_path_for(config, question.question_id)
    try:
        marker_path = Path(str(db_path) + ".ingested.json")
        reuse = (
            config.reuse_stores
            and db_path.is_file()
            and _reuse_marker_matches(marker_path, question, config=config)
        )
        if not reuse:
            _cleanup_store(db_path)
        with question_run(
            question,
            db_path,
            excerpt_source=config.excerpt_source,
            session_label_mode=config.session_label_mode,
            promotion_mode=config.promotion_mode,
            surface=config.surface,
        ) as run:
            if label_sidecar is not None:
                # The one on-disk place raw ids go, next to the checkpoint.
                label_sidecar.append(run.session_labeler)
            ingest_stats = run.ingest(accept_rollups=config.accept_rollups, reuse_store=reuse)
            outcome = run.retrieve(
                max_items=config.max_items,
                context_char_budget=config.context_char_budget,
                pack_format=config.pack_format,
            )
        ingest_record = ingest_stats.to_record()
        if config.reuse_stores:
            ingest_record["reused_store"] = reuse
        record["ingest"] = ingest_record
        record["retrieval"] = outcome.to_record()
        if config.keep_stores and not reuse:
            marker_path.write_text(
                json.dumps(_ingest_marker_payload(question, config), ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        if not config.dry_run:
            assert model is not None and judge is not None  # validated in main()
            prompt = build_answer_prompt(
                context_block=outcome.context_block,
                question=question.question,
                question_date=question.question_date,
                cot=config.cot,
            )
            completion = chat_completion(
                model,
                [{"role": "user", "content": prompt}],
                temperature=GENERATION_TEMPERATURE,
                max_tokens=ANSWER_MAX_TOKENS_COT if config.cot else ANSWER_MAX_TOKENS,
            )
            hypothesis = completion.text.strip()
            record["hypothesis"] = hypothesis
            record["generation"] = {
                "prompt_chars": len(prompt),
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "latency_seconds": round(completion.latency_seconds, 3),
                "retries": completion.retries,
            }
            # Disclosed grounding gate (--verify-grounding): a separate
            # verification call — context + question + answer only, never the
            # gold answer or question type — may replace the hypothesis with
            # the abstention phrasing. It runs uniformly on every question,
            # fails open on verifier errors, and the row keeps the original
            # text plus the full verdict; the flag is in the fingerprint.
            if config.verify_grounding:
                assert verifier is not None  # validated in main()
                verdict = verify_grounding(
                    question=question.question,
                    answer_text=hypothesis,
                    context_block=outcome.context_block,
                    chat_client=make_chat_client(verifier),
                )
                hypothesis, gate_applied = apply_grounding_gate(hypothesis, verdict)
                record["grounding"] = {
                    "verdict": verdict.to_record(),
                    "gate_applied": gate_applied,
                    "original_hypothesis": record["hypothesis"] if gate_applied else None,
                }
                record["hypothesis"] = hypothesis
            judge_result = judge_hypothesis(
                judge,
                question_type=question.question_type,
                question=question.question,
                gold_answer=question.answer,
                hypothesis=hypothesis,
                is_abstention=question.is_abstention,
            )
            record["judge"] = judge_result.to_record()
    except Exception as exc:  # noqa: BLE001 - a bad question must not kill the run
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if not config.keep_stores:
            _cleanup_store(db_path)
    record["completed_at"] = _utc_now_iso()
    return record


# -- aggregation --------------------------------------------------------------


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent / 100.0 * len(ordered)) - 1))
    return ordered[index]


def _accuracy_bucket(records: list[dict[str, object]]) -> dict[str, object]:
    judged = [record for record in records if isinstance(record.get("judge"), dict)]
    correct = sum(1 for record in judged if record["judge"].get("correct") is True)  # type: ignore[index]
    return {
        "questions": len(judged),
        "correct": correct,
        "accuracy": round(correct / len(judged), 4) if judged else None,
    }


def aggregate_records(records: list[dict[str, object]]) -> dict[str, object]:
    ok_records = [record for record in records if record.get("status") == "ok"]
    error_records = [record for record in records if record.get("status") != "ok"]

    per_type: dict[str, dict[str, object]] = {}
    for record in ok_records:
        question_type = str(record.get("question_type"))
        per_type.setdefault(question_type, {"records": []})["records"].append(record)  # type: ignore[union-attr]
    per_type_summary = {
        question_type: _accuracy_bucket(bucket["records"])  # type: ignore[arg-type]
        for question_type, bucket in sorted(per_type.items())
    }

    retrieval_seconds = [
        float(record["retrieval"]["retrieval_seconds"])  # type: ignore[index]
        for record in ok_records
        if isinstance(record.get("retrieval"), dict)
    ]
    context_chars = [
        int(record["retrieval"]["context_chars"])  # type: ignore[index]
        for record in ok_records
        if isinstance(record.get("retrieval"), dict)
    ]
    approx_tokens = [
        int(record["retrieval"]["approx_context_tokens"])  # type: ignore[index]
        for record in ok_records
        if isinstance(record.get("retrieval"), dict)
    ]
    ingest_seconds = [
        float(record["ingest"]["ingest_seconds"])  # type: ignore[index]
        for record in ok_records
        if isinstance(record.get("ingest"), dict)
    ]
    vector_enabled_count = sum(
        1
        for record in ok_records
        if isinstance(record.get("retrieval"), dict) and record["retrieval"].get("vector_enabled") is True  # type: ignore[index]
    )

    overall = _accuracy_bucket(ok_records)
    return {
        "totals": {
            "questions": len(records),
            "ok": len(ok_records),
            "errors": len(error_records),
            "correct": overall["correct"],
            "accuracy": overall["accuracy"],
        },
        "abstention": _accuracy_bucket([record for record in ok_records if record.get("is_abstention") is True]),
        "non_abstention": _accuracy_bucket([record for record in ok_records if record.get("is_abstention") is not True]),
        "per_type": per_type_summary,
        "retrieval": {
            "context_chars_mean": round(sum(context_chars) / len(context_chars), 1) if context_chars else None,
            "approx_context_tokens_mean": round(sum(approx_tokens) / len(approx_tokens), 1) if approx_tokens else None,
            "retrieval_seconds_p50": _percentile(retrieval_seconds, 50),
            "retrieval_seconds_p95": _percentile(retrieval_seconds, 95),
            "ingest_seconds_mean": round(sum(ingest_seconds) / len(ingest_seconds), 3) if ingest_seconds else None,
            "vector_enabled_share": round(vector_enabled_count / len(ok_records), 4) if ok_records else None,
        },
        "failures": [
            {"question_id": record.get("question_id"), "error": record.get("error")} for record in error_records
        ],
    }


def build_report(
    config: RunnerConfig,
    fingerprint: dict[str, object],
    records: list[dict[str, object]],
    *,
    resumed_count: int,
) -> dict[str, object]:
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": _utc_now_iso(),
        "config": fingerprint,
        "resumed_from_checkpoint": resumed_count,
        **aggregate_records(records),
    }


def _retrieval_is_empty(retrieval: dict[str, object], *, surface: str) -> bool:
    """Dry-run emptiness check for one row's retrieval block.

    The context pack surface renders an empty pack as an empty string. The
    recall tool always returns a framed result object, so an empty recall is
    one that returned neither memories nor sources.
    """
    if surface == SURFACE_RECALL:
        return int(retrieval.get("memory_count") or 0) == 0 and int(retrieval.get("source_count") or 0) == 0  # type: ignore[call-overload]
    return int(retrieval.get("context_chars") or 0) == 0  # type: ignore[call-overload]


def validate_run_choices(config: RunnerConfig) -> str | None:
    """Why this combination of run-level choices cannot be run, or ``None``.

    Each refusal is a combination whose fingerprint would describe something
    other than what ran, so they fail before any question is ingested.
    """
    try:
        validate_session_label_mode(config.session_label_mode)
        validate_promotion_mode(config.promotion_mode)
        validate_surface(config.surface)
    except (SessionLabelError, ValueError) as exc:
        return str(exc)
    if config.excerpt_source not in EXCERPT_SOURCES:
        return f"excerpt source {config.excerpt_source!r} is not one of {EXCERPT_SOURCES}"
    if config.accept_rollups and config.promotion_mode == PROMOTION_MODE_SOURCES_ONLY:
        return (
            "--accept-rollups groups promoted memories and cannot be combined with "
            "--promotion-mode sources_only, which promotes none"
        )
    if config.surface == SURFACE_RECALL:
        if config.excerpt_source != EXCERPT_SOURCE_PACK_EXCERPTS:
            return "--surface recall returns the tool's own excerpts; use --excerpt-source pack_excerpts"
        if config.pack_format != DEFAULT_PACK_FORMAT:
            return "--pack-format applies to the context pack surface; recall hands the reader the tool result"
        if not 1 <= config.max_items <= RECALL_MAX_LIMIT:
            return (
                f"--max-items {config.max_items} is outside 1 to {RECALL_MAX_LIMIT}; "
                "--surface recall passes it as the recall tool's limit, which refuses anything else"
            )
        blocker = recall_surface_blocker()
        if blocker is not None:
            return blocker
    return None


# -- CLI -----------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_longmemeval.py",
        description="Score Alice's capture + retrieval pipeline on LongMemEval.",
    )
    parser.add_argument("--variant", choices=VARIANTS, default="s", help="dataset variant (default: s)")
    parser.add_argument("--dataset-file", type=Path, default=None, help="explicit dataset JSON path (overrides --variant lookup)")
    parser.add_argument("--data-dir", type=Path, default=None, help="dataset directory (default: eval/longmemeval/data)")
    subset = parser.add_mutually_exclusive_group()
    subset.add_argument("--limit", type=int, default=None, help="only run the first N questions")
    subset.add_argument(
        "--question-ids",
        type=Path,
        default=None,
        help="file with one question_id per line (blank lines and # comments skipped); "
        "run only those questions — mutually exclusive with --limit",
    )
    parser.add_argument("--resume", action="store_true", help="skip questions already completed in the checkpoint")
    parser.add_argument("--report", type=Path, default=None, help="report JSON path (default: eval/longmemeval/results/)")
    parser.add_argument("--checkpoint", type=Path, default=None, help="checkpoint JSONL path (default: eval/longmemeval/results/)")
    parser.add_argument("--workers", type=int, default=2, help="parallel questions in flight (default: 2; keep small for rate limits)")
    parser.add_argument("--dry-run", action="store_true", help="ingest + retrieval only; no chat model, no judge")
    parser.add_argument("--cot", action="store_true", help="use the official chain-of-thought reading template")
    parser.add_argument(
        "--verify-grounding",
        action="store_true",
        help="disclosed post-generation grounding gate: a separate verification call "
        "(context + question + answer only; never the gold answer) converts answers with "
        "ungrounded load-bearing claims to the abstention phrasing; recorded in the fingerprint",
    )
    parser.add_argument(
        "--accept-rollups",
        action="store_true",
        help="disclosed post-ingest consolidation step: run the product's roll-up "
        "consolidation pass after candidate promotion and review-accept the proposed "
        "cards through the real acceptance path (accept_consolidation_candidate), "
        "modeling the product's human review workflow; recorded in the fingerprint "
        "and per-store counts in each checkpoint row's ingest.rollups block",
    )
    parser.add_argument(
        "--pack-format",
        choices=PACK_FORMATS,
        default=DEFAULT_PACK_FORMAT,
        help="context rendering for the reading template's history slot: 'prose' "
        "(default; byte-identical to published runs) or 'json' (same retrieved "
        "content as a compact structured document); recorded in the fingerprint",
    )
    parser.add_argument(
        "--raw-session-labels",
        action="store_true",
        help="write the dataset's raw session ids into the store and show them to the reader. "
        "Only for reproducing runs made before harness 1.1: LongMemEval names every evidence session "
        "'answer_...', so raw ids tell the reader which sessions hold the evidence. Default: keyed-hash "
        "labels (key id lme-anon-v1), with the label-to-id mapping in a sidecar file next to the checkpoint; "
        "recorded in the fingerprint and on every row",
    )
    parser.add_argument(
        "--excerpt-source",
        choices=EXCERPT_SOURCES,
        default=None,
        help="where excerpt text comes from: 'store_chunks' (the harness reads every chunk of every retrieved "
        "source from the store; no MCP tool offers that) or 'pack_excerpts' (only the excerpt the retrieval "
        "call returns). Default: $ALICE_LME_EXCERPT_SOURCE, else store_chunks, else pack_excerpts under "
        "--surface recall; recorded in the fingerprint and on every row",
    )
    parser.add_argument(
        "--promotion-mode",
        choices=PROMOTION_MODES,
        default=DEFAULT_PROMOTION_MODE,
        help="what happens to the candidate memories capture extracts: 'all_candidates' (default; every one is "
        "force-accepted, as in every published run) or 'sources_only' (none is promoted, which is what a real "
        "import gives a user: sources, not accepted memories); recorded in the fingerprint and on every row",
    )
    parser.add_argument(
        "--surface",
        choices=SURFACES,
        default=DEFAULT_SURFACE,
        help="which retrieval call feeds the reader: 'context_pack' (default; compile_context_pack rendered by "
        "the harness) or 'recall' (the shipped alice_recall MCP tool with its own default limit and fences; "
        "the reader gets the tool's result text, with no harness budget or reference time); recorded in the "
        "fingerprint and on every row",
    )
    parser.add_argument("--max-items", type=int, default=None, help=f"context-pack max_items (default: ${'{'}ALICE_LME_MAX_ITEMS{'}'} or {DEFAULT_MAX_ITEMS})")
    parser.add_argument(
        "--context-char-budget",
        type=int,
        default=None,
        help=f"rendered context budget in characters (default: ${'{'}ALICE_LME_CONTEXT_CHAR_BUDGET{'}'} or {DEFAULT_CONTEXT_CHAR_BUDGET})",
    )
    parser.add_argument("--work-dir", type=Path, default=WORK_DIR, help="scratch dir for per-question SQLite stores")
    parser.add_argument("--keep-stores", action="store_true", help="keep per-question SQLite files for inspection")
    parser.add_argument(
        "--reuse-stores",
        action="store_true",
        help="reuse marker-verified ingested stores in --work-dir (skips session capture; promotion and roll-up acceptance still run; disclosed in the fingerprint and per-row ingest.reused_store)",
    )
    return parser


def _resolve_config(args: argparse.Namespace, *, question_ids: tuple[str, ...] | None = None) -> RunnerConfig | None:
    if args.dataset_file is not None:
        dataset_path = args.dataset_file
        if not dataset_path.is_file():
            print(f"[runner] dataset file does not exist: {dataset_path}", file=sys.stderr)
            return None
    else:
        resolved = (
            resolve_dataset_path(args.variant, data_dir=args.data_dir)
            if args.data_dir is not None
            else resolve_dataset_path(args.variant)
        )
        if resolved is None:
            return None
        dataset_path = resolved
    stem = f"longmemeval_{args.variant}" if args.dataset_file is None else dataset_path.stem
    # Resolved once, here, so the fingerprint records what every worker uses.
    # Raises ValueError for a typo or an incoherent combination (main reports it).
    excerpt_source = resolve_excerpt_source(args.excerpt_source, surface=args.surface)
    return RunnerConfig(
        variant=args.variant,
        dataset_path=dataset_path,
        limit=args.limit,
        question_ids=question_ids,
        question_ids_file=args.question_ids.name if args.question_ids is not None else None,
        resume=args.resume,
        dry_run=args.dry_run,
        cot=args.cot,
        workers=max(1, args.workers),
        max_items=args.max_items if args.max_items is not None else max_items_from_env(),
        context_char_budget=(
            args.context_char_budget if args.context_char_budget is not None else context_char_budget_from_env()
        ),
        work_dir=args.work_dir,
        checkpoint_path=args.checkpoint or RESULTS_DIR / f"{stem}_checkpoint.jsonl",
        report_path=args.report or RESULTS_DIR / f"{stem}_report.json",
        keep_stores=args.keep_stores,
        reuse_stores=args.reuse_stores,
        verify_grounding=args.verify_grounding,
        accept_rollups=args.accept_rollups,
        pack_format=args.pack_format,
        session_label_mode=SESSION_LABEL_MODE_RAW if args.raw_session_labels else DEFAULT_SESSION_LABEL_MODE,
        excerpt_source=excerpt_source,
        promotion_mode=args.promotion_mode,
        surface=args.surface,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    question_ids: tuple[str, ...] | None = None
    if args.question_ids is not None:
        try:
            question_ids = load_question_ids(args.question_ids)
        except ValueError as exc:
            print(f"[runner] {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
    try:
        config = _resolve_config(args, question_ids=question_ids)
    except ValueError as exc:
        print(f"[runner] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if config is not None:
        problem = validate_run_choices(config)
        if problem is not None:
            print(f"[runner] {problem}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
    if config is None:
        if args.dataset_file is not None:
            return EXIT_CONFIG_ERROR
        message = (
            f"[runner] dataset for variant {args.variant!r} not found under eval/longmemeval/data/. "
            "Fetch it with: python eval/longmemeval/fetch.py --variant " + args.variant
        )
        if args.dry_run:
            print(message + " — dry run skipped cleanly.")
            return EXIT_OK
        print(message, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    model = model_config_from_env()
    judge = judge_config_from_env()
    if not config.dry_run and (model is None or judge is None):
        print(
            "[runner] a scored run needs ALICE_LME_MODEL_BASE_URL and ALICE_LME_MODEL "
            "(plus optional ALICE_LME_JUDGE_* overrides); use --dry-run for a model-free smoke.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    if config.dry_run:
        model = None
        judge = None

    verifier: ChatModelConfig | None = None
    if config.verify_grounding and not config.dry_run:
        verifier = verifier_config_from_env()
        if verifier is None:
            print(
                "[runner] --verify-grounding needs a verifier model: set ALICE_LME_VERIFIER_BASE_URL "
                "and ALICE_LME_VERIFIER_MODEL (each falls back to the ALICE_LME_MODEL_* values).",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR

    limit = config.limit
    if config.dry_run and limit is None and config.question_ids is None:
        limit = 2
    try:
        questions = load_dataset(config.dataset_path, limit=limit)
    except LongMemEvalDatasetError as exc:
        print(f"[runner] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if config.question_ids is not None:
        wanted = set(config.question_ids)
        questions = tuple(question for question in questions if question.question_id in wanted)
        missing = sorted(wanted - {question.question_id for question in questions})
        if missing:
            preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            print(
                f"[runner] {len(missing)} question ids from {args.question_ids} are not in the dataset: {preview}",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR
    if not questions:
        print("[runner] dataset contained no questions after --limit", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        # A label collision anywhere in the selected questions stops the run
        # now, before a single question is ingested or paid for.
        validate_dataset_labels(questions, mode=config.session_label_mode)
    except SessionLabelError as exc:
        print(f"[runner] {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    fingerprint = config_fingerprint(config, model=model, judge=judge, verifier=verifier)
    fingerprint_digest = str(fingerprint["digest"])

    existing_records = load_checkpoint(config.checkpoint_path) if config.resume else {}
    done_ids = completed_question_ids(existing_records, mode=config.mode)
    if config.resume and existing_records:
        conflicts = resume_conflicts(
            existing_records, done_ids, config=config, fingerprint_digest=fingerprint_digest
        )
        if conflicts:
            print(
                "[runner] refusing to resume: " + "; ".join(conflicts) + ". "
                "Use a new checkpoint or delete the old one; "
                "mixed-configuration reports are not valid evidence.",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR
    pending = [question for question in questions if question.question_id not in done_ids]

    config.work_dir.mkdir(parents=True, exist_ok=True)
    writer = CheckpointWriter(config.checkpoint_path)
    label_sidecar = (
        None
        if config.session_label_mode == SESSION_LABEL_MODE_RAW
        else SessionLabelSidecar(sidecar_path_for(config.checkpoint_path))
    )
    subset_note = f" subset={config.question_ids_file}({len(config.question_ids)})" if config.question_ids else ""
    print(
        f"[runner] mode={config.mode} variant={config.variant} questions={len(questions)}{subset_note} "
        f"pending={len(pending)} resumed={len(questions) - len(pending)} workers={config.workers} "
        f"fingerprint={fingerprint_digest} labels={config.session_label_mode} "
        f"excerpts={config.excerpt_source} promotion={config.promotion_mode} surface={config.surface}"
    )

    fresh_records: list[dict[str, object]] = []
    started = time.monotonic()
    if pending:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            futures = {
                pool.submit(
                    run_question,
                    question,
                    config,
                    model=model,
                    judge=judge,
                    fingerprint_digest=fingerprint_digest,
                    verifier=verifier,
                    label_sidecar=label_sidecar,
                ): question
                for question in pending
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                record = future.result()
                writer.append(record)
                fresh_records.append(record)
                status = record["status"]
                verdict = ""
                if isinstance(record.get("judge"), dict):
                    verdict = " correct" if record["judge"].get("correct") else " wrong"  # type: ignore[index]
                print(
                    f"[runner] {completed}/{len(pending)} {record['question_id']} "
                    f"({record['question_type']}) {status}{verdict}",
                    flush=True,
                )

    resumed_records = [existing_records[question_id] for question_id in sorted(done_ids) if question_id in existing_records]
    all_records = resumed_records + fresh_records
    report = build_report(config, fingerprint, all_records, resumed_count=len(resumed_records))
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    config.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    totals = report["totals"]
    print(
        f"[runner] done in {time.monotonic() - started:.1f}s — ok={totals['ok']} errors={totals['errors']} "
        f"accuracy={totals['accuracy']} report={config.report_path}"
    )

    if config.dry_run:
        empty_context_ids = [
            str(record["question_id"])
            for record in all_records
            if record.get("status") == "ok"
            and isinstance(record.get("retrieval"), dict)
            and _retrieval_is_empty(record["retrieval"], surface=config.surface)  # type: ignore[arg-type]
            and record.get("is_abstention") is not True
        ]
        if empty_context_ids:
            print(
                f"[runner] dry-run FAILED: empty retrieval context for {sorted(empty_context_ids)}",
                file=sys.stderr,
            )
            return EXIT_RUN_FAILURES
        if int(totals["errors"]) > 0:
            return EXIT_RUN_FAILURES
        print("[runner] dry-run OK: retrieval produced non-empty context for every non-abstention question.")
        return EXIT_OK

    return EXIT_OK if int(totals["errors"]) == 0 else EXIT_RUN_FAILURES


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckpointWriter",
    "EXIT_CONFIG_ERROR",
    "EXIT_OK",
    "EXIT_RUN_FAILURES",
    "HARNESS_VERSION",
    "REPORT_SCHEMA",
    "RESULT_SCHEMA",
    "RunnerConfig",
    "aggregate_records",
    "build_arg_parser",
    "build_report",
    "completed_question_ids",
    "config_fingerprint",
    "load_checkpoint",
    "load_question_ids",
    "main",
    "resume_conflicts",
    "run_question",
    "validate_run_choices",
]
