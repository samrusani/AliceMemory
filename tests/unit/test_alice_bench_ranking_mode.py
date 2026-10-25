"""The harness records the ranking mode a run resolved to, not only the value it set.

Passage mode is the default of ``alice_recall`` on the SQLite store, so a run with ``ALICE_SEARCH_QUALITY`` unset ranks
by passage there. The build record and the run fingerprint of ``scripts/alice_bench.py`` carry ``search_quality`` (the
value the run set, ``unset`` when none) and ``ranking_mode`` (``document`` or ``passage``, asked of the recall edge of
the checkout under test). An unset run is therefore not read as document mode. The showcase manifest records both
as well.

Each test names the edit that must make it fail. The cases ``bench-*-records-only-the-environment-value`` of
``scripts/derived_label_mutations.json`` replay two of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.alice_bench as bench
from alicebot_api.mcp import retrieval as recall_edge
from alicebot_api.source_ranking import SourceRanking
from tests.unit.test_showcase_bench import _note

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "tests" / "fixtures" / "search_quality" / "corpus"

# The value a run sets, and the mode that value resolves to on the SQLite vault the harness builds.
SETTINGS = [
    pytest.param(None, "unset", "passage", id="unset"),
    pytest.param("off", "off", "document", id="off"),
    pytest.param("passage", "passage", "passage", id="passage"),
]


def _build(tmp_path: Path, name: str, setting: str | None) -> tuple[Path, dict[str, Any]]:
    run_dir = tmp_path / name
    flags = [] if setting is None else ["--search-quality", setting]
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS), *flags]) == 0
    manifest = json.loads((run_dir / bench.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return run_dir, manifest


@pytest.mark.parametrize(("setting", "recorded", "mode"), SETTINGS)
def test_the_build_and_the_run_fingerprint_record_the_resolved_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], setting: str | None, recorded: str, mode: str
) -> None:
    """Both records carry the value the run set and the mode it resolved to. They differ for an unset run.

    Mutation: record only the environment value (drop ``ranking_mode``, or write the value of
    ``search_quality`` into it) in ``build_vault`` or in ``fingerprint``.
    """

    run_dir, manifest = _build(tmp_path, "run", setting)
    assert manifest["build"]["search_quality"] == recorded
    assert manifest["build"]["ranking_mode"] == mode

    capsys.readouterr()
    flags = [] if setting is None else ["--search-quality", setting]
    assert bench.main(["fingerprint", "--run-dir", str(run_dir), *flags]) == 0
    fingerprint = json.loads(capsys.readouterr().out)
    assert fingerprint["search_quality"] == recorded
    assert fingerprint["ranking_mode"] == mode
    assert fingerprint["vault_build"]["ranking_mode"] == mode


def test_an_unset_run_is_not_document_mode_on_sqlite(tmp_path: Path) -> None:
    """The case the field exists for: ``search_quality`` says ``unset`` and the mode says ``passage``.

    Mutation: resolve an unset variable to ``document``, as v0.20.2 did.
    """

    _run_dir, unset = _build(tmp_path, "unset", None)
    _run_dir, off = _build(tmp_path, "off", "off")
    assert unset["build"]["search_quality"] == "unset" and unset["build"]["ranking_mode"] == "passage"
    assert off["build"]["search_quality"] == "off" and off["build"]["ranking_mode"] == "document"


@pytest.mark.parametrize(
    ("setting", "mode"), [(None, "passage"), ("off", "document"), ("passage", "passage"), ("on", "passage")]
)
def test_the_mode_comes_from_the_setting_the_run_carries_and_not_from_the_parent(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, mode: str
) -> None:
    """The parent process holds the opposite value. The run's own setting decides, as it does for recall.

    Mutation: read the parent's ``ALICE_SEARCH_QUALITY`` instead of the run's, or skip the scrub.
    """

    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "passage" if setting == "off" else "off")
    assert bench.resolved_ranking_mode(setting) == mode
    fingerprint = bench.fingerprint(
        repo=REPO_ROOT,
        gates=bench.load_gates(),
        session=None,
        manifest=None,
        search_quality=setting,
        question_set_sha256=None,
    )
    assert fingerprint["ranking_mode"] == mode


def test_the_mode_is_asked_of_the_checkout_under_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout whose edge has no store default resolves an unset variable to document, as main did before.

    A checkout with no recall edge at all (v0.20.2) ranks by document whatever the variable says.
    Mutation: hard-code the SQLite default in the harness instead of asking the edge.
    """

    monkeypatch.setattr(
        recall_edge, "recall_source_ranking", lambda _store, _identity: SourceRanking.from_environment(), raising=True
    )
    assert bench.resolved_ranking_mode(None) == "document"
    assert bench.resolved_ranking_mode("off") == "document"
    assert bench.resolved_ranking_mode("passage") == "passage"

    monkeypatch.delattr(recall_edge, "recall_source_ranking")
    assert bench.resolved_ranking_mode(None) == "document"
    assert bench.resolved_ranking_mode("passage") == "document"


def test_a_value_the_product_does_not_know_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: swallow the error and record a mode for a value that recall would refuse."""

    with pytest.raises(bench.BenchError):
        bench.resolved_ranking_mode("sideways")


@pytest.mark.parametrize(
    ("flag", "recorded", "mode"),
    [
        pytest.param([], "unset", "passage", id="unset"),
        pytest.param(["--search-quality", "off"], "off", "document", id="off"),
        pytest.param(["--search-quality", "passage"], "passage", "passage", id="passage"),
    ],
)
def test_the_showcase_manifest_records_the_value_and_the_resolved_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: list[str], recorded: str, mode: str
) -> None:
    """``showcase-heldout`` writes ``search_quality`` as given and ``ranking_mode`` as resolved.

    The search command still compares ``search_quality`` only, so a run built unset is searched unset.
    Mutation: drop ``ranking_mode`` from the showcase manifest, or write the value of ``search_quality`` into it.
    """

    notes = tmp_path / "notes"
    _note(notes, "a.md", "sales", "internal", "professional", "A field note mentions harborlatchtoken.")
    questions = tmp_path / "questions.json"
    question = {
        "id": "h1",
        "kind": "hidden",
        "task": "recall",
        "team": "sales",
        "question": "What token is shared?",
        "must_include": [],
        "must_not_include": [],
        "leak_strings": [],
        "abstain": False,
        "needed_paths": [],
    }
    questions.write_text(json.dumps({"kind": "hidden", "questions": [question]}), encoding="utf-8")
    run = tmp_path / "run"
    arguments = ["--notes", str(notes), "--questions", str(questions), "--run-dir", str(run), "--embeddings", "off"]
    built = bench.main(["showcase-heldout", *arguments, *flag])
    assert built == 0, capsys.readouterr()
    manifest = json.loads((run / "showcase.json").read_text(encoding="utf-8"))
    assert manifest["search_quality"] == recorded
    assert manifest["ranking_mode"] == mode
