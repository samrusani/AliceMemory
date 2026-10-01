"""Session labels: the keyed hash, its collision check, and the sidecar file.

Model-free and network-free. Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_session_labels.py -q

Every test names, in its docstring, the change that must make it fail; each of
those changes was made by hand and the test seen to fail before this file was
committed.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from longmemeval import session_labels  # noqa: E402
from longmemeval.dataset import parse_question  # noqa: E402
from longmemeval.session_labels import (  # noqa: E402
    ANON_KEY_ID,
    ANON_LABEL_PATTERN,
    DEFAULT_SESSION_LABEL_MODE,
    SESSION_LABEL_MODE_ANONYMISED,
    SESSION_LABEL_MODE_RAW,
    SessionLabelCollisionError,
    SessionLabelError,
    SessionLabeler,
    SessionLabelSidecar,
    anon_session_label,
    key_id_for_mode,
    session_labeler_for_question,
    sidecar_path_for,
    validate_dataset_labels,
)


def _question(session_ids: list[str], answer_ids: list[str], question_id: str = "q1"):
    return parse_question(
        {
            "question_id": question_id,
            "question_type": "multi-session",
            "question": "What breed is the dog?",
            "answer": "golden retriever",
            "question_date": "2023/06/01 (Thu) 10:00",
            "haystack_dates": ["2023/05/01 (Mon) 10:00"] * len(session_ids),
            "haystack_session_ids": session_ids,
            "haystack_sessions": [[{"role": "user", "content": "hello"}]] * len(session_ids),
            "answer_session_ids": answer_ids,
        }
    )


def test_default_is_anonymised_and_raw_is_the_opt_in() -> None:
    """Fails if the default flips to raw: new runs would show the evidence label again."""
    assert DEFAULT_SESSION_LABEL_MODE == SESSION_LABEL_MODE_ANONYMISED
    assert session_labels.SESSION_LABEL_MODES == (SESSION_LABEL_MODE_ANONYMISED, SESSION_LABEL_MODE_RAW)
    assert key_id_for_mode(SESSION_LABEL_MODE_ANONYMISED) == ANON_KEY_ID == "lme-anon-v1"
    assert key_id_for_mode(SESSION_LABEL_MODE_RAW) is None


def test_label_is_pinned_to_known_values() -> None:
    """Fails if the key, the NUL separator, the digest, the truncation or the prefix changes.

    The expected strings were computed outside this code base with
    ``hmac.new(key, question_id + NUL + session_id, sha256).hexdigest()[:10]``.
    A silent change would make new runs incomparable with earlier anonymised
    runs, which is the one property a constant key exists to give.
    """
    assert anon_session_label("q1", "answer_5f3c9a71") == "Sce712bade2"
    assert anon_session_label("q2", "answer_5f3c9a71") == "S3b07d90404"
    assert anon_session_label("118b2229", "answer_40a90d51") == "Sbb5d5641ad"


def test_label_hides_every_dataset_prefix_and_is_scoped_by_question() -> None:
    """Fails if a label keeps the raw id (or its prefix) or ignores the question id."""
    for session_id in ("answer_5f3c9a71", "sharegpt_Kp3xQ0", "ultrachat_88123", "7c1e0b9d4a6f"):
        label = anon_session_label("q1", session_id)
        assert ANON_LABEL_PATTERN.match(label), label
        assert session_id not in label
        assert not label.startswith(("answer", "sharegpt", "ultrachat"))
    assert anon_session_label("q1", "answer_x") != anon_session_label("q2", "answer_x")


def test_unknown_key_id_and_mode_fail_loudly() -> None:
    """Fails if a typo falls back silently to some key or to raw labels."""
    with pytest.raises(SessionLabelError, match="unknown session label key id"):
        anon_session_label("q1", "s1", key_id="lme-anon-v9")
    with pytest.raises(SessionLabelError, match="not one of"):
        SessionLabeler("q1", ["s1"], mode="anonymous")


def test_collision_inside_one_question_fails_loudly() -> None:
    """Fails if a collision is merged or ignored instead of raised.

    One hex digit gives 16 labels, so forty distinct ids must collide. Two
    sessions sharing a label would make them indistinguishable to the reader and
    corrupt coverage comparisons, so the labeler refuses.
    """
    with pytest.raises(SessionLabelCollisionError, match="two different session ids"):
        SessionLabeler("q1", [f"session_{index}" for index in range(40)], hex_chars=1)


def test_the_same_raw_id_twice_is_not_a_collision() -> None:
    """Fails if a repeated identical id is reported as a collision."""
    labeler = SessionLabeler("q1", ["s1", "s1", "s2"])
    assert len(labeler.label_map()) == 2


def test_dataset_validation_stops_a_collision_before_any_question_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if validate_dataset_labels stops checking, so a collision would surface mid-run."""
    monkeypatch.setattr(session_labels, "anon_session_label", lambda *_args, **_kwargs: "Sconstant00")
    with pytest.raises(SessionLabelCollisionError):
        validate_dataset_labels([_question(["a", "b"], ["a"])], mode=SESSION_LABEL_MODE_ANONYMISED)
    validate_dataset_labels([_question(["a", "b"], ["a"])], mode=SESSION_LABEL_MODE_RAW)  # raw never collides


def test_labeler_maps_both_ways_and_covers_evidence_ids() -> None:
    """Fails if the labeler forgets the evidence ids or cannot map a label back."""
    question = _question(["filler_1", "answer_1"], ["answer_1", "answer_not_in_haystack"])
    labeler = session_labeler_for_question(question)
    # Registered up front, before anything asks for it: the evidence ids join
    # the collision check and the sidecar mapping even when they are absent
    # from the haystack.
    assert sorted(labeler.label_map().values()) == ["answer_1", "answer_not_in_haystack", "filler_1"]
    label = labeler.label_for("answer_1")
    assert labeler.raw_id(label) == "answer_1"
    assert labeler.is_known_label(label)
    assert labeler.label_for("answer_not_in_haystack") == labeler.labels_for(["answer_not_in_haystack"]).pop()
    with pytest.raises(SessionLabelError, match="not a label"):
        labeler.raw_id("Sffffffffff")


def test_raw_mode_is_the_identity() -> None:
    """Fails if raw mode rewrites ids: old runs could no longer be reproduced."""
    labeler = SessionLabeler("q1", ["answer_1", "filler_1"], mode=SESSION_LABEL_MODE_RAW)
    assert labeler.label_for("answer_1") == "answer_1"
    assert labeler.require_reader_safe("anything") == "anything"


def test_reader_fence_refuses_anything_that_is_not_a_label_of_this_question() -> None:
    """Fails if require_reader_safe lets a raw id, or another question's label, through."""
    labeler = SessionLabeler("q1", ["answer_1", "filler_1"])
    label = labeler.label_for("answer_1")
    assert labeler.require_reader_safe(label) == label
    for not_a_label in ("answer_1", "filler_1", anon_session_label("q2", "answer_1"), "Sffffffffff"):
        with pytest.raises(SessionLabelError, match="not an anonymised label"):
            labeler.require_reader_safe(not_a_label)


def test_sidecar_holds_the_mapping_and_raw_mode_writes_none(tmp_path: Path) -> None:
    """Fails if the sidecar drops the mapping, lands elsewhere, or is written for raw labels."""
    checkpoint = tmp_path / "run_checkpoint.jsonl"
    path = sidecar_path_for(checkpoint)
    assert path == tmp_path / "run_checkpoint.session-labels.jsonl"
    sidecar = SessionLabelSidecar(path)
    labeler = SessionLabeler("q1", ["answer_1", "filler_1"])
    sidecar.append(labeler)
    sidecar.append(SessionLabeler("q2", ["answer_2"], mode=SESSION_LABEL_MODE_RAW))
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    record = lines[0]
    assert record["schema"] == session_labels.SIDECAR_SCHEMA
    assert record["question_id"] == "q1"
    assert record["session_label_mode"] == SESSION_LABEL_MODE_ANONYMISED
    assert record["session_label_key_id"] == ANON_KEY_ID
    assert record["labels"] == labeler.label_map()
    assert set(record["labels"].values()) == {"answer_1", "filler_1"}
