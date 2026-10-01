"""The run-level choices that decide what a LongMemEval score measures.

Session labels are covered by test_session_labels.py and
test_session_label_leak_guard.py. This file covers the other three choices
(excerpt source, promotion mode, surface), where all four are recorded
(fingerprint, every checkpoint row, the store-reuse marker), and the refusal to
mix rows that differ in any of them.

Model-free, embedding-free and network-free. Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_run_choices.py -q

Every test names, in its docstring, the change that must make it fail; each of
those changes was made by hand and the test seen to fail before this file was
committed.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from alicebot_api.mcp.types import _RECALL_DEFAULT_LIMIT  # noqa: E402
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext, call_mcp_tool  # noqa: E402
from alicebot_api.recall_framing import serialize_mcp_tool_result  # noqa: E402

from longmemeval import adapter, coverage_probe, pack_formats, runner, session_labels  # noqa: E402
from longmemeval.dataset import SYNTHETIC_FIXTURE_PATH, load_dataset, parse_question  # noqa: E402
from longmemeval.session_labels import (  # noqa: E402
    SESSION_LABEL_MODE_ANONYMISED,
    SESSION_LABEL_MODE_RAW,
    session_labeler_for_question,
    sidecar_path_for,
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _config(tmp_path: Path, **overrides: object) -> runner.RunnerConfig:
    values: dict[str, object] = {
        "variant": "s",
        "dataset_path": SYNTHETIC_FIXTURE_PATH,
        "limit": None,
        "question_ids": None,
        "question_ids_file": None,
        "resume": False,
        "dry_run": True,
        "cot": False,
        "workers": 1,
        "max_items": 8,
        "context_char_budget": 12_000,
        "work_dir": tmp_path / "work",
        "checkpoint_path": tmp_path / "ckpt.jsonl",
        "report_path": tmp_path / "report.json",
        "keep_stores": False,
    }
    values.update(overrides)
    return runner.RunnerConfig(**values)  # type: ignore[arg-type]


def _recall_config(tmp_path: Path, **overrides: object) -> runner.RunnerConfig:
    return _config(
        tmp_path,
        surface=adapter.SURFACE_RECALL,
        excerpt_source=adapter.EXCERPT_SOURCE_PACK_EXCERPTS,
        **overrides,
    )


def _main_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--dataset-file",
        str(SYNTHETIC_FIXTURE_PATH),
        "--dry-run",
        "--workers",
        "1",
        "--work-dir",
        str(tmp_path / "work"),
        "--checkpoint",
        str(tmp_path / "ckpt.jsonl"),
        "--report",
        str(tmp_path / "report.json"),
        *extra,
    ]


# -- the choices are recorded -------------------------------------------------------------


def test_fingerprint_records_all_four_choices_and_each_one_changes_the_digest(tmp_path: Path) -> None:
    """Fails if any choice is dropped from the fingerprint or stops moving the digest.

    Mutation: delete ``"excerpt_source"``, ``"promotion_mode"``, ``"surface"``,
    ``"session_label_mode"`` or ``"session_label_key_id"`` from
    ``config_fingerprint``. Before harness 1.1 none of them was there: a run
    that read store chunks and one that read pack excerpts had the same digest.
    """
    base = _config(tmp_path)
    fingerprint = runner.config_fingerprint(base, model=None, judge=None)
    assert fingerprint["harness_version"] == "1.1"
    assert fingerprint["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    assert fingerprint["session_label_key_id"] == "lme-anon-v1"
    assert fingerprint["excerpt_source"] == adapter.EXCERPT_SOURCE_STORE_CHUNKS
    assert fingerprint["promotion_mode"] == adapter.PROMOTION_MODE_ALL_CANDIDATES
    assert fingerprint["surface"] == adapter.SURFACE_CONTEXT_PACK

    variants = {
        "raw labels": replace(base, session_label_mode=SESSION_LABEL_MODE_RAW),
        "pack excerpts": replace(base, excerpt_source=adapter.EXCERPT_SOURCE_PACK_EXCERPTS),
        "sources only": replace(base, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY),
        "recall": _recall_config(tmp_path),
    }
    digests = {"base": fingerprint["digest"]}
    for name, config in variants.items():
        digests[name] = runner.config_fingerprint(config, model=None, judge=None)["digest"]
    assert len(set(digests.values())) == len(digests), digests

    raw = runner.config_fingerprint(variants["raw labels"], model=None, judge=None)
    assert raw["session_label_mode"] == SESSION_LABEL_MODE_RAW and raw["session_label_key_id"] is None


def test_recall_fingerprint_does_not_claim_a_budget_or_a_rendered_pack(tmp_path: Path) -> None:
    """Fails if a recall run records a context budget or pack format it never used.

    Mutation: record ``config.context_char_budget`` or ``config.pack_format``
    unconditionally. The recall surface hands the reader the tool's result as
    returned, so neither applies.
    """
    fingerprint = runner.config_fingerprint(_recall_config(tmp_path), model=None, judge=None)
    assert fingerprint["surface"] == adapter.SURFACE_RECALL
    assert fingerprint["context_char_budget"] is None
    assert fingerprint["pack_format"] == adapter.RECALL_RESULT_FORMAT
    pack = runner.config_fingerprint(_config(tmp_path), model=None, judge=None)
    assert pack["context_char_budget"] == 12_000 and pack["pack_format"] == pack_formats.PACK_FORMAT_PROSE


def test_every_row_carries_the_choices_even_when_the_question_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if ``run_question`` stops stamping the choices on a row.

    Mutation: remove ``**config.run_choices()`` from the record in
    ``run_question``. The second half sets an agent key, which makes the recall
    surface refuse, so the row is an error row and must still carry them.
    """
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    config = _config(tmp_path)
    config.work_dir.mkdir()
    record = runner.run_question(question, config, model=None, judge=None, fingerprint_digest="d")
    assert record["status"] == "ok", record["error"]
    expected = {
        "harness_version": "1.1",
        "session_label_mode": SESSION_LABEL_MODE_ANONYMISED,
        "session_label_key_id": "lme-anon-v1",
        "excerpt_source": adapter.EXCERPT_SOURCE_STORE_CHUNKS,
        "promotion_mode": adapter.PROMOTION_MODE_ALL_CANDIDATES,
        "surface": adapter.SURFACE_CONTEXT_PACK,
    }
    assert {key: record[key] for key in expected} == expected

    monkeypatch.setenv("ALICE_AGENT_API_KEY", "not-a-real-key")
    errored = runner.run_question(question, _recall_config(tmp_path), model=None, judge=None, fingerprint_digest="d")
    assert errored["status"] == "error" and "ALICE_AGENT_API_KEY" in str(errored["error"])
    assert errored["surface"] == adapter.SURFACE_RECALL
    assert errored["excerpt_source"] == adapter.EXCERPT_SOURCE_PACK_EXCERPTS


def test_environment_excerpt_source_now_reaches_the_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the excerpt source is read per question instead of once into the config.

    Mutation: leave ``excerpt_source`` out of ``_resolve_config`` and
    ``run_question``'s ``question_run`` call. Before 1.1 the variable was read
    by each worker and never recorded, so a resume with it unset silently mixed
    the two readers.
    """
    monkeypatch.setenv(adapter.EXCERPT_SOURCE_ENV, adapter.EXCERPT_SOURCE_PACK_EXCERPTS)
    assert runner.main(_main_args(tmp_path)) == runner.EXIT_OK
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["config"]["excerpt_source"] == adapter.EXCERPT_SOURCE_PACK_EXCERPTS
    rows = [json.loads(line) for line in (tmp_path / "ckpt.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["excerpt_source"] for row in rows} == {adapter.EXCERPT_SOURCE_PACK_EXCERPTS}


# -- resume refuses to mix ---------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    (
        ["--raw-session-labels"],
        ["--excerpt-source", "pack_excerpts"],
        ["--promotion-mode", "sources_only"],
        ["--surface", "recall"],
    ),
    ids=("labels", "excerpt_source", "promotion", "surface"),
)
def test_resume_refuses_a_checkpoint_made_with_other_choices(
    tmp_path: Path, flags: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Fails if ``--resume`` accepts completed rows written under different choices.

    Mutation: make ``resume_conflicts`` return ``[]``.
    """
    assert runner.main(_main_args(tmp_path)) == runner.EXIT_OK
    capsys.readouterr()
    assert runner.main(_main_args(tmp_path, "--resume")) == runner.EXIT_OK  # same choices resume fine
    capsys.readouterr()
    assert runner.main(_main_args(tmp_path, "--resume", *flags)) == runner.EXIT_CONFIG_ERROR
    assert "refusing to resume" in capsys.readouterr().err


def test_resume_names_the_field_even_when_the_digest_matches(tmp_path: Path) -> None:
    """Fails if the per-row fields are not checked on their own.

    The digest alone would accept these rows. They are what a row written by
    older code, or edited by hand, looks like: the digest is right and one
    field is wrong or missing. Mutation: delete the per-field loop in
    ``resume_conflicts``.
    """
    config = _config(tmp_path)
    digest = "same-digest"
    good = {"fingerprint_digest": digest, "status": "ok", "mode": "dry_run", **config.run_choices()}
    wrong_labels = {**good, "session_label_mode": SESSION_LABEL_MODE_RAW}
    missing_surface = {key: value for key, value in good.items() if key != "surface"}
    records = {"q_good": good, "q_labels": wrong_labels, "q_surface": missing_surface}
    conflicts = runner.resume_conflicts(records, set(records), config=config, fingerprint_digest=digest)
    assert len(conflicts) == 2, conflicts
    assert any("1 completed rows differ in session_label_mode" in conflict and "'raw'" in conflict for conflict in conflicts)
    assert any("1 completed rows differ in surface" in conflict and "<missing>" in conflict for conflict in conflicts)
    assert runner.resume_conflicts({"q_good": good}, {"q_good"}, config=config, fingerprint_digest=digest) == []


def test_resume_still_checks_the_digest(tmp_path: Path) -> None:
    """Fails if the fingerprint digest comparison is dropped from ``resume_conflicts``."""
    config = _config(tmp_path)
    row = {"fingerprint_digest": "old", "status": "ok", "mode": "dry_run", **config.run_choices()}
    conflicts = runner.resume_conflicts({"q": row}, {"q"}, config=config, fingerprint_digest="new")
    assert conflicts == ["1 completed rows were produced with a different config fingerprint"]


# -- the store-reuse marker -----------------------------------------------------------------


def test_reuse_marker_separates_label_and_promotion_modes(tmp_path: Path) -> None:
    """Fails if a store ingested under one mode can be reused under another.

    Mutation: drop ``session_label_mode`` or ``promotion_mode`` from the marker
    payload. A store ingested raw holds raw ids in its text and metadata; one
    ingested ``all_candidates`` holds accepted memories.
    """
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    base = _config(tmp_path)
    marker = runner._ingest_marker_payload(question, base)
    assert marker["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    assert marker["promotion_mode"] == adapter.PROMOTION_MODE_ALL_CANDIDATES
    assert runner._ingest_marker_payload(question, replace(base, session_label_mode=SESSION_LABEL_MODE_RAW)) != marker
    assert (
        runner._ingest_marker_payload(question, replace(base, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY))
        != marker
    )
    marker_path = tmp_path / "q.ingested.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert runner._reuse_marker_matches(marker_path, question, config=base)
    assert not runner._reuse_marker_matches(
        marker_path, question, config=replace(base, session_label_mode=SESSION_LABEL_MODE_RAW)
    )


# -- excerpt source resolution -----------------------------------------------------------------


def test_excerpt_source_resolution_order_and_the_recall_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if the flag, the variable or the recall rule resolve differently.

    Mutation: let ``resolve_excerpt_source`` accept ``store_chunks`` for the
    recall surface (the recall tool returns excerpts only, so a recorded
    ``store_chunks`` would describe a reader that was never used).
    """
    resolve = adapter.resolve_excerpt_source
    assert resolve() == adapter.EXCERPT_SOURCE_STORE_CHUNKS
    assert resolve(surface=adapter.SURFACE_RECALL) == adapter.EXCERPT_SOURCE_PACK_EXCERPTS
    monkeypatch.setenv(adapter.EXCERPT_SOURCE_ENV, adapter.EXCERPT_SOURCE_PACK_EXCERPTS)
    assert resolve() == adapter.EXCERPT_SOURCE_PACK_EXCERPTS
    assert resolve(adapter.EXCERPT_SOURCE_STORE_CHUNKS) == adapter.EXCERPT_SOURCE_STORE_CHUNKS  # flag beats variable
    with pytest.raises(ValueError, match="recall surface"):
        resolve(adapter.EXCERPT_SOURCE_STORE_CHUNKS, surface=adapter.SURFACE_RECALL)
    with pytest.raises(ValueError, match="not one of"):
        resolve("pack")
    with pytest.raises(ValueError, match="not one of"):
        resolve(surface="pack")


def test_incoherent_combinations_are_refused_before_any_question_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fails if a combination whose fingerprint would misdescribe the run is accepted.

    Mutation: remove the matching branch from ``validate_run_choices``.
    """
    base = _config(tmp_path)
    assert runner.validate_run_choices(base) is None
    assert runner.validate_run_choices(_recall_config(tmp_path)) is None
    problem = runner.validate_run_choices(
        replace(base, accept_rollups=True, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY)
    )
    assert problem is not None and "sources_only" in problem
    problem = runner.validate_run_choices(replace(_recall_config(tmp_path), pack_format=pack_formats.PACK_FORMAT_JSON))
    assert problem is not None and "recall" in problem
    problem = runner.validate_run_choices(replace(_recall_config(tmp_path), excerpt_source=adapter.EXCERPT_SOURCE_STORE_CHUNKS))
    assert problem is not None and "pack_excerpts" in problem
    assert runner.validate_run_choices(replace(base, surface="rag")) is not None
    assert runner.validate_run_choices(replace(base, promotion_mode="some")) is not None
    assert runner.validate_run_choices(replace(base, session_label_mode="anonymous")) is not None

    monkeypatch.setenv("ALICE_AGENT_API_KEY", "not-a-real-key")
    assert "ALICE_AGENT_API_KEY" in str(runner.validate_run_choices(_recall_config(tmp_path)))
    assert runner.main(_main_args(tmp_path, "--surface", "recall")) == runner.EXIT_CONFIG_ERROR
    assert "ALICE_AGENT_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "ckpt.jsonl").exists()


def test_label_collision_stops_the_run_before_any_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fails if a collision is only discovered question by question, after spending.

    Mutation: remove the ``validate_dataset_labels`` call from ``main``.
    """
    monkeypatch.setattr(session_labels, "anon_session_label", lambda *_args, **_kwargs: "Sconstant00")
    assert runner.main(_main_args(tmp_path)) == runner.EXIT_CONFIG_ERROR
    assert "two different session ids" in capsys.readouterr().err
    assert not (tmp_path / "ckpt.jsonl").exists()
    assert runner.main(_main_args(tmp_path, "--raw-session-labels")) == runner.EXIT_OK  # raw never collides


def test_sidecar_is_written_next_to_the_checkpoint_unless_labels_are_raw(tmp_path: Path) -> None:
    """Fails if the sidecar is missing for an anonymised run, or written for a raw one.

    Mutation: drop the ``label_sidecar`` argument from the ``run_question``
    submit in ``main``.
    """
    assert runner.main(_main_args(tmp_path)) == runner.EXIT_OK
    sidecar = sidecar_path_for(tmp_path / "ckpt.jsonl")
    lines = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    questions = {question.question_id: question for question in load_dataset(SYNTHETIC_FIXTURE_PATH)}
    assert {line["question_id"] for line in lines} == set(questions)
    for line in lines:
        assert line["labels"] == session_labeler_for_question(questions[line["question_id"]]).label_map()

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    assert runner.main(_main_args(raw_dir, "--raw-session-labels")) == runner.EXIT_OK
    assert not sidecar_path_for(raw_dir / "ckpt.jsonl").exists()
    rows = [json.loads(line) for line in (raw_dir / "ckpt.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["session_label_mode"] for row in rows} == {SESSION_LABEL_MODE_RAW}
    assert {row["session_label_key_id"] for row in rows} == {None}


# -- promotion mode ----------------------------------------------------------------------------


def _ingest_synthetic(tmp_path: Path, **choices: str):
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3", **choices) as run:
        stats = run.ingest()
        candidates = len(run.store.list_memories(status="candidate"))
        active = len(run.store.list_memories(status="active"))
        outcome = run.retrieve(max_items=8, context_char_budget=12_000)
    return stats, candidates, active, outcome


def test_default_promotion_accepts_every_candidate_as_before(tmp_path: Path) -> None:
    """Fails if the default stops reproducing the published runs' store.

    Mutation: make ``ingest`` skip ``_promote_candidate_memories`` for the
    default mode.
    """
    assert adapter.DEFAULT_PROMOTION_MODE == adapter.PROMOTION_MODE_ALL_CANDIDATES
    stats, candidates, active, outcome = _ingest_synthetic(tmp_path)
    assert stats.candidate_memory_count > 0
    assert stats.promoted_memory_count == stats.candidate_memory_count
    assert candidates == 0 and active == stats.promoted_memory_count
    assert outcome.memory_count > 0


def test_sources_only_leaves_the_store_as_an_import_does(tmp_path: Path) -> None:
    """Fails if ``sources_only`` promotes anything or lets a memory reach the reader.

    Mutation: ignore ``promotion_mode`` in ``ingest`` (always promote).
    """
    stats, candidates, active, outcome = _ingest_synthetic(
        tmp_path, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY
    )
    assert stats.candidate_memory_count > 0, "capture extracted no candidates, the test would prove nothing"
    assert stats.promoted_memory_count == 0
    assert candidates == stats.candidate_memory_count and active == 0
    assert outcome.memory_count == 0
    assert outcome.source_count > 0 and outcome.excerpt_count > 0, "sources must still be retrievable"
    assert "Facts Alice remembers" not in outcome.context_block


def test_sources_only_never_calls_the_promotion_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if the promotion step runs at all under ``sources_only``.

    Mutation: call ``_promote_candidate_memories`` unconditionally and discard
    the count.
    """
    calls: list[object] = []
    monkeypatch.setattr(
        adapter.QuestionRun, "_promote_candidate_memories", lambda self, **_kwargs: calls.append(self) or 0
    )
    _ingest_synthetic(tmp_path, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY)
    assert calls == []


def test_sources_only_cannot_accept_rollups(tmp_path: Path) -> None:
    """Fails if roll-up acceptance is allowed on a store with no promoted memories.

    Mutation: remove the guard at the top of ``QuestionRun.ingest``.
    """
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(
        question, tmp_path / "q.sqlite3", promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY
    ) as run:
        with pytest.raises(ValueError, match="sources_only"):
            run.ingest(accept_rollups=True)


# -- the recall surface --------------------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.tool_calls: list[tuple[MCPRuntimeContext, str, dict[str, object]]] = []
        self.source_searches = 0
        self.pack_compiles = 0


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy()
    real_call = adapter.call_mcp_tool

    def call(context: MCPRuntimeContext, *, name: str, arguments: dict[str, object]):  # type: ignore[no-untyped-def]
        spy.tool_calls.append((context, name, dict(arguments)))
        return real_call(context, name=name, arguments=arguments)

    real_search = adapter.VNextRetrievalService.search_source_excerpts

    def search(self, **kwargs):  # type: ignore[no-untyped-def]
        spy.source_searches += 1
        return real_search(self, **kwargs)

    def compile_pack(self, request):  # type: ignore[no-untyped-def]
        spy.pack_compiles += 1
        raise AssertionError("the recall surface compiled a context pack")

    monkeypatch.setattr(adapter, "call_mcp_tool", call)
    monkeypatch.setattr(adapter.VNextRetrievalService, "search_source_excerpts", search)
    monkeypatch.setattr(adapter.VNextRetrievalService, "compile_context_pack", compile_pack)
    return spy


def _recall_retrieve(tmp_path: Path, question_index: int = 0, **choices: str):
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[question_index]
    db_path = tmp_path / f"q{question_index}.sqlite3"
    with adapter.question_run(
        question,
        db_path,
        surface=adapter.SURFACE_RECALL,
        excerpt_source=adapter.EXCERPT_SOURCE_PACK_EXCERPTS,
        **choices,
    ) as run:
        run.ingest()
        outcome = run.retrieve()  # no explicit limit, no budget: the tool's own defaults
    return question, db_path, outcome


def test_recall_surface_goes_through_the_shipped_tool_not_the_context_pack(tmp_path: Path, spy: _Spy) -> None:
    """Fails if the recall surface stops calling ``alice_recall`` through ``call_mcp_tool``.

    Mutation: replace the ``call_mcp_tool`` call in ``_retrieve_via_recall``
    with ``compile_context_pack`` (the spy raises on it).
    """
    question, db_path, outcome = _recall_retrieve(tmp_path)
    assert spy.pack_compiles == 0
    assert spy.source_searches == 1, "the tool's own source excerpt search did not run"
    [(context, name, arguments)] = spy.tool_calls
    assert name == "alice_recall"
    assert arguments == {"query": question.question, "limit": _RECALL_DEFAULT_LIMIT, "debug": True}
    assert context.user_id == adapter.LME_USER_ID
    assert context.agent_identity is None and context.agent_identity_resolved is False
    assert context.database_url.startswith("sqlite://")
    assert Path(unquote(urlparse(context.database_url).path)) == db_path.resolve()
    assert outcome.pack_format == adapter.RECALL_RESULT_FORMAT
    assert outcome.source_count > 0 and outcome.excerpt_count > 0, "the committed ingest was not visible to the tool"


def test_the_blocker_message_names_the_variable_the_tool_reads() -> None:
    """Fails if the literal name in the blocker message drifts from the variable the tool reads.

    The message spells the name as a literal (see ``recall_surface_blocker``).
    Mutation: change the literal, or the constant in ``alicebot_api``.
    """
    assert AGENT_API_KEY_ENV == "ALICE_AGENT_API_KEY"


def test_recall_default_limit_is_the_tools_default() -> None:
    """Fails if the harness default drifts from the tool's default limit.

    Mutation: change ``DEFAULT_MAX_ITEMS`` to 9. The recall surface applies the
    run's ``max_items`` as the tool's ``limit``; the harness default and the
    tool default are the same number on purpose, and this makes a change on
    either side a decision instead of an accident.
    """
    assert adapter.DEFAULT_MAX_ITEMS == _RECALL_DEFAULT_LIMIT == 8


def test_recall_context_is_exactly_the_text_the_mcp_server_would_return(tmp_path: Path) -> None:
    """Fails if the reader is handed anything but the tool result.

    Calls the tool again, without ``debug`` and with no harness involved, and
    compares the serialized text. Mutation: keep the ``retrieval`` block the
    ``debug`` flag adds, or render the result through the harness's own prose.
    """
    question, db_path, outcome = _recall_retrieve(tmp_path)
    context = MCPRuntimeContext(database_url="sqlite://" + str(db_path.resolve()), user_id=adapter.LME_USER_ID)
    direct = call_mcp_tool(context, name="alice_recall", arguments={"query": question.question})
    assert "retrieval" not in direct
    assert outcome.context_block == serialize_mcp_tool_result(direct)
    assert '"retrieval"' not in outcome.context_block
    assert outcome.vector_stage.startswith("disabled")  # read from the debug block before it was removed
    assert outcome.vector_enabled is False
    assert outcome.memory_count == direct["count"]
    assert outcome.source_count == direct["source_count"]


def test_recall_surface_sees_promoted_memories_only_when_they_were_promoted(tmp_path: Path) -> None:
    """Fails if the two promotion modes look the same through the recall tool.

    Mutation: ignore ``promotion_mode`` in ``ingest``.
    """
    (tmp_path / "all").mkdir()
    (tmp_path / "src").mkdir()
    _question, _db, promoted = _recall_retrieve(tmp_path / "all")
    _question, _db, sources_only = _recall_retrieve(
        tmp_path / "src", promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY
    )
    assert promoted.memory_count > 0
    assert sources_only.memory_count == 0 and sources_only.source_count > 0


def test_recall_surface_refuses_what_it_cannot_honour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if a recall run can record a reader, format or identity it did not use.

    Mutation: remove the matching guard in ``QuestionRun.__init__``,
    ``retrieve`` or ``recall_surface_blocker``.
    """
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with pytest.raises(ValueError, match="pack_excerpts"):
        adapter.QuestionRun(
            question, object(), surface=adapter.SURFACE_RECALL, excerpt_source=adapter.EXCERPT_SOURCE_STORE_CHUNKS  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="store's file path"):
        adapter.QuestionRun(
            question, object(), surface=adapter.SURFACE_RECALL, excerpt_source=adapter.EXCERPT_SOURCE_PACK_EXCERPTS  # type: ignore[arg-type]
        ).retrieve()
    with adapter.question_run(
        question,
        tmp_path / "q.sqlite3",
        surface=adapter.SURFACE_RECALL,
        excerpt_source=adapter.EXCERPT_SOURCE_PACK_EXCERPTS,
    ) as run:
        run.ingest()
        with pytest.raises(ValueError, match="pack_format"):
            run.retrieve(pack_format=pack_formats.PACK_FORMAT_JSON)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", "not-a-real-key")
        with pytest.raises(ValueError, match="ALICE_AGENT_API_KEY"):
            run.retrieve()


def test_dry_run_empty_check_for_recall_counts_results_not_characters(tmp_path: Path) -> None:
    """Fails if an empty recall result passes the dry-run check as non-empty.

    The tool always returns a framed object, so ``context_chars`` is never 0.
    Mutation: use ``context_chars`` for the recall surface too.
    """
    empty = {"context_chars": 250, "memory_count": 0, "source_count": 0}
    full = {"context_chars": 250, "memory_count": 0, "source_count": 2}
    assert runner._retrieval_is_empty(empty, surface=adapter.SURFACE_RECALL) is True
    assert runner._retrieval_is_empty(full, surface=adapter.SURFACE_RECALL) is False
    assert runner._retrieval_is_empty({"context_chars": 0}, surface=adapter.SURFACE_CONTEXT_PACK) is True
    assert runner._retrieval_is_empty(empty, surface=adapter.SURFACE_CONTEXT_PACK) is False
    assert runner.main(_main_args(tmp_path, "--surface", "recall")) == runner.EXIT_OK


# -- the coverage probe ----------------------------------------------------------------------------


def test_coverage_probe_maps_the_dataset_side_through_the_label_function() -> None:
    """Fails if the probe compares labels with raw evidence ids.

    Mutation: replace ``labeler.labels_for(question.answer_session_ids)`` in
    ``coverage_row`` with ``set(question.answer_session_ids)``. Every anonymised
    run would then report zero coverage, which looks like a retrieval failure.
    """
    question = parse_question(
        {
            "question_id": "cov_q",
            "question_type": "multi-session",
            "question": "q?",
            "answer": "a",
            "question_date": "2023/06/01 (Thu) 10:00",
            "haystack_dates": ["2023/05/01 (Mon) 10:00"] * 2,
            "haystack_session_ids": ["answer_one", "sharegpt_two"],
            "haystack_sessions": [[{"role": "user", "content": "hello"}]] * 2,
            "answer_session_ids": ["answer_one"],
        }
    )
    labeler = session_labeler_for_question(question)
    row = coverage_probe.coverage_row(
        question, {labeler.label_for("answer_one")}, session_label_mode=SESSION_LABEL_MODE_ANONYMISED
    )
    assert row["any_coverage"] is True and row["all_coverage"] is True
    assert row["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    wrong_side = coverage_probe.coverage_row(question, {"answer_one"}, session_label_mode=SESSION_LABEL_MODE_ANONYMISED)
    assert wrong_side["any_coverage"] is False
    assert wrong_side["missed_session_ids"] == [labeler.label_for("answer_one")]
    assert "answer_one" not in json.dumps(row) and "answer_one" not in json.dumps(wrong_side)
    with pytest.raises(TypeError):
        coverage_probe.coverage_row(question, set())  # type: ignore[call-arg]


def test_coverage_probe_does_not_reuse_a_store_across_label_modes(tmp_path: Path) -> None:
    """Fails if the probe reuses a store ingested under the other label mode.

    Mutation: drop ``session_label_mode`` from the probe's marker comparison.
    """
    work_dir = tmp_path / "stores"

    def probe(*extra: str) -> list[dict[str, object]]:
        out = tmp_path / f"rows{len(extra)}.jsonl"
        code = coverage_probe.main(
            ["--dataset-file", str(SYNTHETIC_FIXTURE_PATH), "--work-dir", str(work_dir), "--out", str(out), "--workers", "1", *extra]
        )
        assert code == coverage_probe.EXIT_OK
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    first = probe()
    assert {row["session_label_mode"] for row in first} == {SESSION_LABEL_MODE_ANONYMISED}
    assert first[0]["any_coverage"] is True and first[0]["reused_store"] is False
    raw = probe("--raw-session-labels")
    assert {row["session_label_mode"] for row in raw} == {SESSION_LABEL_MODE_RAW}
    assert raw[0]["any_coverage"] is True
    assert raw[0]["reused_store"] is False, "a store ingested with hashed labels was reused for a raw-label probe"


# -- the corrected sentence ----------------------------------------------------------------------------


def test_pack_formats_no_longer_claims_that_no_labels_enter_the_document() -> None:
    """Fails if the false sentence comes back.

    Mutation: restore "no benchmark labels enter the document" in the
    ``pack_formats`` module docstring. It was false for both formats before
    harness 1.1: the raw session id, which carries the evidence label, was in
    every excerpt record and in the prose excerpt header.
    """
    doc = " ".join((pack_formats.__doc__ or "").split())
    assert "no benchmark labels enter the document" not in doc
    assert "answer_" in doc and "session_labels" in doc and "was false" in doc
