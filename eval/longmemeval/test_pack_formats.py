"""Tests for the structured (JSON) pack format — model-free and network-free.

Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_pack_formats.py -q

Covers content equivalence between the prose and JSON renderings, budget
discipline on the SERIALIZED JSON length, fingerprint disclosure of the pack
format, default-prose byte-identity, and the sha-pins on the official
reading templates (pack format shapes the history slot's content only).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from longmemeval import adapter, pack_formats, runner  # noqa: E402
from longmemeval.dataset import SYNTHETIC_FIXTURE_PATH, load_dataset, parse_question  # noqa: E402
from longmemeval.session_labels import SESSION_LABEL_MODE_RAW  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        adapter.CONTEXT_CHAR_BUDGET_ENV,
        adapter.MAX_ITEMS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


# -- fixtures (self-contained; no reliance on sibling test modules) -----------

_SESSIONS = {
    "src-a": ("session_a", "2023/05/20 (Sat) 14:10"),
    "src-b": ("session_b", "2023/05/01 (Mon) 09:00"),
    "src-c": ("session_c", "2023/06/02 (Fri) 18:30"),
}
_PROSE_EXCERPT_HEADER = re.compile(
    r"\[Session (?P<session_id>[^ |]+) \| (?P<date>[^|]+) \| excerpt (?P<index>\d+)\]"
)


class _StubStore:
    """Just enough store surface for the context renderers."""

    def __init__(self, chunks_by_source: dict[str, list[str]]) -> None:
        self._chunks_by_source = chunks_by_source

    def list_source_chunks(self, source_id: str) -> list[dict[str, object]]:
        return [
            {"text": text, "chunk_index": index}
            for index, text in enumerate(self._chunks_by_source.get(source_id, []))
        ]

    def get_source(self, source_id: str) -> dict[str, object] | None:
        if source_id not in _SESSIONS:
            return None
        session_id, date = _SESSIONS[source_id]
        return {"metadata_json": {"session_id": session_id, "session_date": date}}


def _padded(text: str, length: int = 150) -> str:
    return (text + " " + "lorem ipsum " * 30)[:length]


def _run() -> adapter.QuestionRun:
    question = parse_question(
        {
            "question_id": "q_pack_format",
            "question_type": "multi-session",
            "question": "Did I adopt a golden retriever puppy from the shelter?",
            "answer": "yes",
            "question_date": "2023/07/01 (Sat) 10:00",
            "haystack_dates": ["2023/05/20 (Sat) 14:10"],
            "haystack_session_ids": ["session_a"],
            "haystack_sessions": [[{"role": "user", "content": "hello"}]],
            "answer_session_ids": ["session_a"],
        }
    )
    store = _StubStore(
        {
            "src-a": [
                _padded(f"chunk {index}: adopt the golden retriever puppy near the shelter downtown")
                for index in range(10)
            ],
            "src-b": [_padded("the golden retriever went to training class on Monday morning")],
            "src-c": [_padded("the puppy came home from the shelter and slept all afternoon")],
        }
    )
    # Stub stores hold the literal session ids written in this module, so the packing
    # logic is tested in raw mode; labelling is tested in test_session_label_leak_guard.py.
    return adapter.QuestionRun(question, store, session_label_mode=SESSION_LABEL_MODE_RAW)  # type: ignore[arg-type]


def _pack() -> dict[str, object]:
    return {
        "relevant_memories": [
            {
                "id": "mem-1",
                "canonical_text": "The user adopted a golden retriever puppy.",
                "metadata_json": {"source_id": "src-a", "provenance_role": "user"},
            },
            {
                "id": "mem-2",
                "canonical_text": "The puppy came home from the shelter.",
                "metadata_json": {"source_id": "src-c", "provenance_role": "user"},
                "validity": {"valid_from": "2023-06-02T00:00:00+00:00"},
            },
        ],
        "sources": [{"id": "src-a"}, {"id": "src-b"}, {"id": "src-c"}],
    }


def _prose_facts(block: str) -> list[tuple[str, str]]:
    """``(date, text)`` per prose fact line."""
    facts: list[tuple[str, str]] = []
    for line in block.splitlines():
        match = re.match(r"- \[(?P<date>[^\]]+)\] (?P<text>.+)$", line)
        if match:
            text = re.sub(r" \[[^\]]*\]$", "", match.group("text"))  # strip validity suffix
            facts.append((match.group("date"), text))
    return facts


def _prose_excerpts(block: str) -> list[tuple[str, str, int, str]]:
    """``(session_id, date, excerpt_index, text)`` per prose excerpt."""
    excerpts: list[tuple[str, str, int, str]] = []
    matches = list(_PROSE_EXCERPT_HEADER.finditer(block))
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(block)
        text = block[match.end() : end].strip("\n")
        text = text.split("\n\nNote: no stored memories")[0].rstrip("\n")
        excerpts.append(
            (
                match.group("session_id"),
                match.group("date").strip(),
                int(match.group("index")),
                text,
            )
        )
    return excerpts


# -- content equivalence -------------------------------------------------------


def test_json_and_prose_carry_identical_content_under_roomy_budget() -> None:
    run = _run()
    budget = 8_000
    prose_block, prose_count = run._render_context_block(_pack(), budget=budget)
    json_block, json_count = run._render_context_json(_pack(), budget=budget)

    document = json.loads(json_block)
    assert set(document) == {"memories", "session_excerpts"}

    # Memories: same claims, same session dates, same order as the fact lines.
    assert [(m["date"], m["claim"]) for m in document["memories"]] == _prose_facts(prose_block)
    assert [m["id"] for m in document["memories"]] == ["mem-1", "mem-2"]

    # Excerpts: same sessions, dates, ordinals, and texts in the same
    # (oldest-session-first) order.
    json_excerpts = [
        (e["session_id"], e["date"], e["excerpt_index"], e["excerpt"])
        for e in document["session_excerpts"]
    ]
    assert json_count == len(json_excerpts) == prose_count
    assert json_excerpts == _prose_excerpts(prose_block)


def test_json_renders_grounding_notes_within_budget() -> None:
    run = _run()
    pack = _pack() | {"grounding": {"unsupported_entities": ["Marcus Chen"], "checked": 2}}
    budget = 2_000
    block, _count = run._render_context_json(pack, budget=budget)
    document = json.loads(block)
    assert document["notes"] == ['Note: no stored memories mention "Marcus Chen".']
    assert len(block) <= budget


def test_json_is_deterministic() -> None:
    run = _run()
    first = run._render_context_json(_pack(), budget=3_000)
    second = run._render_context_json(_pack(), budget=3_000)
    assert first == second


def test_both_formats_render_empty_pack_as_empty_string() -> None:
    run = _run()
    empty = {"relevant_memories": [], "sources": []}
    assert run._render_context_block(empty, budget=1_000) == ("", 0)
    assert run._render_context_json(empty, budget=1_000) == ("", 0)


# -- budget discipline (serialized length) --------------------------------------


def test_json_budget_applies_to_serialized_length() -> None:
    run = _run()
    for budget in (900, 1_200, 2_000, 5_000):
        block, count = run._render_context_json(_pack(), budget=budget)
        assert len(block) <= budget, f"budget {budget} exceeded: {len(block)}"
        document = json.loads(block)  # always well-formed JSON
        assert len(document["session_excerpts"]) == count


def test_json_pass1_guarantees_each_source_an_excerpt() -> None:
    run = _run()
    # Roomy enough for exactly three serialized excerpt records plus the
    # envelope, so pass 1's per-source guarantee is the whole selection.
    block, count = run._render_context_json(_pack(), budget=1_250)
    document = json.loads(block)
    sessions = [e["session_id"] for e in document["session_excerpts"]]
    assert count == 3
    assert sessions.count("session_a") == sessions.count("session_b") == sessions.count("session_c") == 1
    # Oldest-session-first, exactly like prose.
    assert sessions == ["session_b", "session_a", "session_c"]


# -- record fields ---------------------------------------------------------------


def test_memory_record_carries_provenance_and_annotations() -> None:
    run = _run()
    block, _count = run._render_context_json(_pack(), budget=5_000)
    memories = json.loads(block)["memories"]
    assert memories[0] == {
        "id": "mem-1",
        "claim": "The user adopted a golden retriever puppy.",
        # The prose fact line's ISO-normalized date (see _iso_fact_date),
        # byte-identical across formats since the date-precompute merge.
        "date": "2023-05-20 (Sat) 14:10",
        "date_iso": "2023-05-20",
        "provenance_role": "user",
    }
    # validity is passed through when present, omitted cleanly when not.
    assert memories[1]["validity"] == {"valid_from": "2023-06-02T00:00:00+00:00"}
    assert "validity" not in memories[0]
    assert "currency" not in memories[0]


def test_memory_record_passthrough_of_sibling_fields() -> None:
    record = pack_formats.memory_record(
        {
            "id": "mem-x",
            "metadata_json": {"provenance_role": "assistant"},
            "validity": {"superseded": True},
            "currency": {"as_of": "2023-08-01"},
        },
        claim="claim text",
        date="undated",
    )
    assert record == {
        "id": "mem-x",
        "claim": "claim text",
        "date": "undated",  # unparseable date: no fabricated date_iso
        "provenance_role": "assistant",
        "validity": {"superseded": True},
        "currency": {"as_of": "2023-08-01"},
    }
    bare = pack_formats.memory_record({"id": "mem-y", "validity": {}}, claim="c", date="2023/01/02 (Mon) 08:00")
    assert bare == {"id": "mem-y", "claim": "c", "date": "2023/01/02 (Mon) 08:00", "date_iso": "2023-01-02"}


def test_derived_timeline_lands_in_json_as_top_level_field() -> None:
    derived_lines = [
        "[derived] 2023-05-20 (session date) was 42 days before the question date.",
        "[derived] 2 dated items span 13 days (2023-05-20 to 2023-06-02).",
    ]
    run = _run()
    pack = _pack()
    pack["derived_values"] = {"reference_time": "2023-07-01T10:00:00+00:00", "lines": derived_lines}

    prose_block, _ = run._render_context_block(pack, budget=5_000)
    json_block, _ = run._render_context_json(pack, budget=5_000)
    document = json.loads(json_block)

    # The same bounded "[derived]" lines in both formats: prose under its
    # section header, JSON as the top-level derived_timeline array.
    assert document["derived_timeline"] == derived_lines
    assert adapter.DERIVED_SECTION_HEADER in prose_block
    for line in derived_lines:
        assert line in prose_block

    # Uncharged in JSON exactly like prose: excerpt admission is identical
    # with or without the block.
    without = json.loads(run._render_context_json(_pack(), budget=5_000)[0])
    assert document["session_excerpts"] == without["session_excerpts"]
    # And absent derived values leave the document shape untouched.
    assert "derived_timeline" not in without


def test_iso_date_prefix_shapes() -> None:
    assert pack_formats.iso_date_prefix("2023/05/28 (Sun) 20:27") == "2023-05-28"
    assert pack_formats.iso_date_prefix("2023/5/8 (Mon) 11:00") == "2023-05-08"
    assert pack_formats.iso_date_prefix("2023-08-01T00:00:00+00:00") == "2023-08-01"
    assert pack_formats.iso_date_prefix("undated") is None
    assert pack_formats.iso_date_prefix("") is None
    assert pack_formats.iso_date_prefix(None) is None


# -- default-prose byte-identity and end-to-end dispatch -------------------------


def test_default_retrieve_is_prose_byte_identical(tmp_path: Path) -> None:
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3") as run:
        run.ingest()
        default = run.retrieve(max_items=8, context_char_budget=12_000)
        prose = run.retrieve(max_items=8, context_char_budget=12_000, pack_format="prose")
        as_json = run.retrieve(max_items=8, context_char_budget=12_000, pack_format="json")

    assert default.context_block == prose.context_block
    assert default.context_sha256 == prose.context_sha256
    assert default.pack_format == prose.pack_format == "prose"
    # Off-flag checkpoint rows must not drift: no pack_format key by default.
    assert "pack_format" not in default.to_record()
    assert as_json.to_record()["pack_format"] == "json"

    document = json.loads(as_json.context_block)
    # Same retrieval feeding both formats: identical memory rows in order.
    assert [m["id"] for m in document["memories"]] == list(as_json.memory_ids) == list(prose.memory_ids)
    assert as_json.context_sha256 == hashlib.sha256(as_json.context_block.encode("utf-8")).hexdigest()


def test_retrieve_rejects_unknown_pack_format(tmp_path: Path) -> None:
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3") as run:
        with pytest.raises(ValueError, match="pack_format"):
            run.retrieve(pack_format="yaml")


# -- runner flag and fingerprint disclosure --------------------------------------


def test_runner_flag_parses_and_defaults_to_prose() -> None:
    parser = runner.build_arg_parser()
    assert parser.parse_args([]).pack_format == "prose"
    assert parser.parse_args(["--pack-format", "json"]).pack_format == "json"
    with pytest.raises(SystemExit):
        parser.parse_args(["--pack-format", "yaml"])


def test_fingerprint_discloses_pack_format(tmp_path: Path) -> None:
    def config_with(pack_format: str) -> runner.RunnerConfig:
        return runner.RunnerConfig(
            variant="s",
            dataset_path=SYNTHETIC_FIXTURE_PATH,
            limit=None,
            question_ids=None,
            question_ids_file=None,
            resume=False,
            dry_run=True,
            cot=False,
            workers=1,
            max_items=8,
            context_char_budget=12_000,
            work_dir=tmp_path,
            checkpoint_path=tmp_path / "c.jsonl",
            report_path=tmp_path / "r.json",
            keep_stores=False,
            pack_format=pack_format,
        )

    prose = runner.config_fingerprint(config_with("prose"), model=None, judge=None)
    as_json = runner.config_fingerprint(config_with("json"), model=None, judge=None)
    assert prose["pack_format"] == "prose"
    assert as_json["pack_format"] == "json"
    # A JSON-pack run can never masquerade as a prose run: the digests differ.
    assert prose["digest"] != as_json["digest"]


def test_runner_dry_run_with_json_packs(tmp_path: Path) -> None:
    exit_code = runner.main(
        [
            "--dry-run",
            "--dataset-file",
            str(SYNTHETIC_FIXTURE_PATH),
            "--pack-format",
            "json",
            "--work-dir",
            str(tmp_path / "work"),
            "--checkpoint",
            str(tmp_path / "checkpoint.jsonl"),
            "--report",
            str(tmp_path / "report.json"),
            "--workers",
            "1",
        ]
    )
    assert exit_code == runner.EXIT_OK
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["config"]["pack_format"] == "json"
    rows = [
        json.loads(line)
        for line in (tmp_path / "checkpoint.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows and all(row["retrieval"]["pack_format"] == "json" for row in rows)


# -- official templates stay byte-frozen ------------------------------------------


def test_official_reading_templates_sha_pins_hold() -> None:
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert digest(adapter.ANSWER_PROMPT_TEMPLATE) == (
        "e427ff913456e51a132ec865b1b5038d562bdc36890976943ad421cc9b365c9d"
    )
    assert digest(adapter.ANSWER_PROMPT_TEMPLATE_COT) == (
        "9e2b3110622929ab896696dd8937231c7436740ec3b9586f653f97346e19ab2c"
    )
    # Pack format shapes the history slot's CONTENT only; the surrounding
    # instruction text is the official template either way.
    for template in (adapter.ANSWER_PROMPT_TEMPLATE, adapter.ANSWER_PROMPT_TEMPLATE_COT):
        assert "json" not in template.lower()
