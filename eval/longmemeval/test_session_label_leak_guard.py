"""Leak guard: no raw LongMemEval session id reaches the store, the reader or a row.

Model-free, embedding-free and network-free (no provider is configured, so
retrieval is FTS-only and nothing calls out). Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_session_label_leak_guard.py -q

Background. LongMemEval names the id of every evidence session ``answer_...``
and no filler session so; filler ids carry ``sharegpt_`` or ``ultrachat_``
prefixes or are plain hex. Harnesses before 1.1 copied the raw id into the
stored source title, the first paragraph of chunk 0, the metadata and the
header above every excerpt, so the reader could see which sessions were the
evidence. These tests fail if any raw id, or any of the three prefixes, shows
up in any of those places again, in every excerpt source, both pack formats
and the recall surface.

The fixture has two evidence sessions (ids start with ``answer_``) and three
filler sessions (``sharegpt_``, ``ultrachat_`` and plain hex), all about the
question's topic so that every one of them is retrieved. A test that never
retrieves the session it hides would pass for the wrong reason, so the main
test asserts all five were retrieved and that their labels are what the reader saw.

Mutation record (made by hand against the committed code, one at a time; each
mutation was confirmed to land, the named test seen to fail, and the file
restored by copying the saved copy back):

* ``render_session_text(label, ...)`` -> ``render_session_text(session_id, ...)``
  in ``QuestionRun.ingest``: ``test_no_raw_session_id_in_store_reader_or_row``,
  store scan (chunk text, found in ``source_chunks``).
* the source ``title=`` built from ``session_id`` instead of ``label``: same
  test, store scan (found in the event log, which records the title, and in
  ``sources``).
* ``external_id=`` built from ``session_id``: same test, store scan.
* ``metadata_json["session_id"] = session_id``: same test, store scan.
* the in-memory ``_source_sessions[...] = (label, date)`` set to ``(session_id,
  date)`` at ingest: same test, the check that the five retrieved labels are the
  five labels of the sessions.
* ``_session_label`` returning the raw id read back from the store metadata
  (the path a reopened store takes): same test, ``reopened_store`` cases.
* the prose header in ``_render_context_block`` built from the raw id: same
  test, prose cases, reader scan.
* the JSON excerpt record built from the raw id in ``_render_context_json``:
  same test, JSON cases, reader scan.
* ``source_session_ids`` in ``retrieve`` built from the raw ids: same test,
  label-set check; the same in ``_retrieve_via_recall``: the recall cases.
* ``require_reader_safe`` removed from ``_session_label``:
  ``test_a_store_ingested_raw_cannot_be_read_as_anonymised``.
* ``label_sidecar.append`` skipped in ``run_question``:
  ``test_checkpoint_row_and_sidecar_split_the_labels_from_the_raw_ids``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from longmemeval import adapter, runner  # noqa: E402
from longmemeval.dataset import LongMemEvalQuestion, parse_question  # noqa: E402
from longmemeval.pack_formats import PACK_FORMAT_JSON, PACK_FORMAT_PROSE  # noqa: E402
from longmemeval.session_labels import (  # noqa: E402
    SESSION_LABEL_MODE_ANONYMISED,
    SESSION_LABEL_MODE_RAW,
    SessionLabelError,
    SessionLabelSidecar,
    session_labeler_for_question,
    sidecar_path_for,
)


QUESTION_ID = "leakguard_q1"
EVIDENCE_IDS = ("answer_5f3c9a71", "answer_b7d02e44")
FILLER_IDS = ("sharegpt_Kp3xQ0", "ultrachat_88123", "7c1e0b9d4a6f")
RAW_IDS = EVIDENCE_IDS + FILLER_IDS
KNOWN_PREFIXES = ("answer_", "sharegpt_", "ultrachat_")
QUESTION_TEXT = "What breed is the user's dog Biscuit?"
QUESTION_DATE = "2023/07/01 (Sat) 10:00"

_SESSION_TEXT = {
    "sharegpt_Kp3xQ0": ("2023/04/10 (Mon) 23:07", "My neighbour asked what breed the dog Biscuit is, I said probably a retriever."),
    "answer_5f3c9a71": ("2023/05/20 (Sat) 14:10", "My dog Biscuit is a three-year-old golden retriever, a breed that loves the lake."),
    "ultrachat_88123": ("2023/05/28 (Sun) 09:45", "The shelter said the dog breed matters less than training, so Biscuit goes to class."),
    "7c1e0b9d4a6f": ("2023/06/02 (Fri) 18:30", "Biscuit the dog chewed a shoe today, typical of the breed at that age."),
    "answer_b7d02e44": ("2023/06/15 (Thu) 08:20", "Biscuit, my golden retriever dog, is the friendliest breed at the park."),
}


def _leak_question() -> LongMemEvalQuestion:
    session_ids = list(_SESSION_TEXT)
    return parse_question(
        {
            "question_id": QUESTION_ID,
            "question_type": "multi-session",
            "question": QUESTION_TEXT,
            "answer": "golden retriever",
            "question_date": QUESTION_DATE,
            "haystack_dates": [_SESSION_TEXT[session_id][0] for session_id in session_ids],
            "haystack_session_ids": session_ids,
            "haystack_sessions": [
                [
                    {"role": "user", "content": _SESSION_TEXT[session_id][1]},
                    {"role": "assistant", "content": "Thanks for sharing that about the dog."},
                ]
                for session_id in session_ids
            ],
            "answer_session_ids": list(EVIDENCE_IDS),
        }
    )


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider, no agent key: retrieval is FTS-only and the recall tool runs as the local operator."""
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_RERANKER_API_KEY",
        "ALICE_AGENT_API_KEY",
        adapter.CONTEXT_CHAR_BUDGET_ENV,
        adapter.MAX_ITEMS_ENV,
        adapter.EXCERPT_SOURCE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _leaks(text: str) -> list[str]:
    """Every raw id and known dataset prefix found in ``text``."""
    return [needle for needle in (*RAW_IDS, *KNOWN_PREFIXES) if needle in text]


def _store_dump(connection: sqlite3.Connection) -> dict[str, str]:
    """Every value of every table in the store, as text (blobs read as raw bytes).

    Scanning the whole store, not just the tables the adapter writes, is the
    point: a raw id that reached a memory row, an event, a trace or the full
    text index would be found too. The caller asserts the tables that matter
    were readable and non-empty, so an unreadable table cannot hide a leak.
    """
    names = [
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    ]
    dump: dict[str, str] = {}
    for name in names:
        values: list[str] = []
        for row in connection.execute(f'SELECT * FROM "{name}"'):
            for value in row.values():
                values.append(value.decode("latin-1") if isinstance(value, bytes) else str(value))
        dump[name] = "\n".join(values)
    return dump


# (surface, excerpt source, pack format): every way the reader's context is produced.
_CONFIGS = [
    pytest.param(adapter.SURFACE_CONTEXT_PACK, adapter.EXCERPT_SOURCE_STORE_CHUNKS, PACK_FORMAT_PROSE, id="pack-store_chunks-prose"),
    pytest.param(adapter.SURFACE_CONTEXT_PACK, adapter.EXCERPT_SOURCE_STORE_CHUNKS, PACK_FORMAT_JSON, id="pack-store_chunks-json"),
    pytest.param(adapter.SURFACE_CONTEXT_PACK, adapter.EXCERPT_SOURCE_PACK_EXCERPTS, PACK_FORMAT_PROSE, id="pack-pack_excerpts-prose"),
    pytest.param(adapter.SURFACE_CONTEXT_PACK, adapter.EXCERPT_SOURCE_PACK_EXCERPTS, PACK_FORMAT_JSON, id="pack-pack_excerpts-json"),
    pytest.param(adapter.SURFACE_RECALL, adapter.EXCERPT_SOURCE_PACK_EXCERPTS, PACK_FORMAT_PROSE, id="recall"),
]


def _ingest_and_retrieve(
    tmp_path: Path,
    *,
    surface: str,
    excerpt_source: str,
    pack_format: str,
    promotion_mode: str,
    session_label_mode: str,
    reopen: bool = False,
) -> tuple[dict[str, str], adapter.RetrievalOutcome, list[str]]:
    """Ingest the fixture, dump the store, then retrieve.

    ``reopen`` retrieves from a second, fresh ``QuestionRun`` over the committed
    store file, which is what ``--reuse-stores`` and the coverage probe do. A
    fresh run has no in-memory session map, so the labels it shows the reader
    are read back from the stored source metadata, a different code path from
    the one a same-run retrieve takes.
    """
    question = _leak_question()
    choices = {
        "excerpt_source": excerpt_source,
        "session_label_mode": session_label_mode,
        "promotion_mode": promotion_mode,
        "surface": surface,
    }
    db_path = tmp_path / "q.sqlite3"
    with adapter.question_run(question, db_path, **choices) as run:
        run.ingest()
        stored = _store_dump(run.store.conn)  # before any retrieval: what ingest alone wrote
        if not reopen:
            outcome = run.retrieve(max_items=8, context_char_budget=12_000, pack_format=pack_format)
        labels = [run.session_labeler.label_for(session_id) for session_id in RAW_IDS]
    if reopen:
        with adapter.question_run(question, db_path, **choices) as reopened:
            outcome = reopened.retrieve(max_items=8, context_char_budget=12_000, pack_format=pack_format)
    return stored, outcome, labels


@pytest.mark.parametrize("reopen", (False, True), ids=("same_run", "reopened_store"))
@pytest.mark.parametrize("promotion_mode", adapter.PROMOTION_MODES)
@pytest.mark.parametrize(("surface", "excerpt_source", "pack_format"), _CONFIGS)
def test_no_raw_session_id_in_store_reader_or_row(
    tmp_path: Path, surface: str, excerpt_source: str, pack_format: str, promotion_mode: str, reopen: bool
) -> None:
    """Fails if any raw id or prefix reaches a stored table, the reader's prompt or a row.

    Mutation: pass the raw ``session_id`` instead of ``label`` at any one of the
    four store entry points in ``QuestionRun.ingest`` (chunk text, title,
    external id, metadata), or make ``_session_label``, the prose header, the
    JSON excerpt record or ``source_session_ids`` use ``labeler.raw_id(...)``.
    The module docstring records each mutation and the assertion that caught it.
    """
    stored, outcome, labels = _ingest_and_retrieve(
        tmp_path,
        surface=surface,
        excerpt_source=excerpt_source,
        pack_format=pack_format,
        promotion_mode=promotion_mode,
        session_label_mode=SESSION_LABEL_MODE_ANONYMISED,
        reopen=reopen,
    )

    # Not vacuous: the tables that carry text were read and hold the sessions,
    # and every one of the five sessions was retrieved and is visible by label.
    assert {"sources", "source_chunks"} <= set(stored), sorted(stored)
    assert stored["sources"] and stored["source_chunks"]
    assert len(outcome.source_session_ids) == len(RAW_IDS) == len(set(outcome.source_session_ids))
    assert set(outcome.source_session_ids) == set(labels)
    assert all(label in outcome.context_block for label in labels), "a retrieved session is invisible to the reader"

    # 1. The store, scanned before retrieval ran.
    for table, text in stored.items():
        assert _leaks(text) == [], f"raw session id or prefix in store table {table!r}"

    # 2. The reader: the context block and the full answer prompt built from it.
    prompt = adapter.build_answer_prompt(
        context_block=outcome.context_block, question=QUESTION_TEXT, question_date=QUESTION_DATE, cot=True
    )
    assert _leaks(outcome.context_block) == []
    assert _leaks(prompt) == []

    # 3. The checkpoint row's retrieval block.
    assert _leaks(json.dumps(outcome.to_record())) == []


def test_the_scan_is_sensitive_raw_labels_are_found_everywhere_they_are_written(tmp_path: Path) -> None:
    """Positive control. Fails if the scan above could not see a leak at all.

    With raw labels switched on (the flag for reproducing old runs) every raw id
    must be found in the store and in the reader's context, and the evidence
    prefix must be found too. If this ever stops finding them, the guard test
    would pass on a store full of raw ids.
    """
    stored, outcome, _labels = _ingest_and_retrieve(
        tmp_path,
        surface=adapter.SURFACE_CONTEXT_PACK,
        excerpt_source=adapter.EXCERPT_SOURCE_STORE_CHUNKS,
        pack_format=PACK_FORMAT_PROSE,
        promotion_mode=adapter.PROMOTION_MODE_ALL_CANDIDATES,
        session_label_mode=SESSION_LABEL_MODE_RAW,
    )
    store_text = "\n".join(stored.values())
    assert set(RAW_IDS) <= {needle for needle in RAW_IDS if needle in store_text}
    assert set(RAW_IDS) <= {needle for needle in RAW_IDS if needle in outcome.context_block}
    assert "answer_" in outcome.context_block
    for table in ("sources", "source_chunks"):
        assert _leaks(stored[table]), table
    assert set(outcome.source_session_ids) == set(RAW_IDS)


def test_a_store_ingested_raw_cannot_be_read_as_anonymised(tmp_path: Path) -> None:
    """Fails if the read-back fence is removed from ``_session_label``.

    Mutation: delete the ``require_reader_safe`` call in ``_session_label``. A
    store left over from a raw-label run would then print raw ids in the
    excerpt headers of a run that records itself as anonymised.
    """
    question = _leak_question()
    db_path = tmp_path / "q.sqlite3"
    with adapter.question_run(question, db_path, session_label_mode=SESSION_LABEL_MODE_RAW) as run:
        run.ingest()
    with adapter.question_run(question, db_path, session_label_mode=SESSION_LABEL_MODE_ANONYMISED) as run:
        with pytest.raises(SessionLabelError, match="not an anonymised label"):
            run.retrieve(max_items=8, context_char_budget=12_000)


def test_checkpoint_row_and_sidecar_split_the_labels_from_the_raw_ids(tmp_path: Path) -> None:
    """Fails if a raw id lands in the row, or the sidecar stops holding the mapping.

    Runs the real ``run_question`` in dry-run mode (no chat model, no judge) and
    checks both files a run writes: the row carries labels only, and the sidecar
    next to the checkpoint is the one place raw ids appear, keyed by label.

    Mutation: build ``source_session_ids`` from ``labeler.raw_id(...)`` (row
    scan fails), or skip ``label_sidecar.append`` in ``run_question`` (sidecar
    assertion fails).
    """
    question = _leak_question()
    checkpoint = tmp_path / "run_checkpoint.jsonl"
    config = runner.RunnerConfig(
        variant="s",
        dataset_path=tmp_path / "unused.json",
        limit=None,
        question_ids=None,
        question_ids_file=None,
        resume=False,
        dry_run=True,
        cot=False,
        workers=1,
        max_items=8,
        context_char_budget=12_000,
        work_dir=tmp_path / "work",
        checkpoint_path=checkpoint,
        report_path=tmp_path / "report.json",
        keep_stores=False,
    )
    config.work_dir.mkdir()
    sidecar = SessionLabelSidecar(sidecar_path_for(checkpoint))

    record = runner.run_question(
        question, config, model=None, judge=None, fingerprint_digest="x", label_sidecar=sidecar
    )

    assert record["status"] == "ok", record["error"]
    row_text = json.dumps(record)
    assert _leaks(row_text) == []
    assert record["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    labeler = session_labeler_for_question(question)
    row_labels = record["retrieval"]["provenance"]["source_session_ids"]  # type: ignore[index]
    assert len(row_labels) == len(RAW_IDS) and all(labeler.is_known_label(label) for label in row_labels)

    mapping_lines = sidecar_path_for(checkpoint).read_text(encoding="utf-8").splitlines()
    assert len(mapping_lines) == 1
    mapping = json.loads(mapping_lines[0])
    assert mapping["question_id"] == QUESTION_ID
    assert set(mapping["labels"].values()) == set(RAW_IDS)
    assert set(row_labels) <= set(mapping["labels"])
