"""Session labels: keep LongMemEval's session ids away from the reader.

Why this exists. In the LongMemEval_s data the id of every evidence session
starts with ``answer_`` and no filler session id does, so a harness that copies
the dataset's session id into the text it ingests or the headers it renders
tells the reader model which sessions hold the evidence (the filler ids carry a
second, weaker label of their own: ``sharegpt_`` and ``ultrachat_`` prefixes).
Every published LongMemEval run before harness 1.1 did exactly that. The runs
are kept as evidence and labelled "raw"; new runs hide the ids.

The scheme. ``anon_session_label(question_id, session_id)`` is ``"S"`` plus the
first 10 hex characters of ``HMAC-SHA256(key, question_id + NUL + session_id)``.

* The key is a constant experiment key identified by ``ANON_KEY_ID`` and
  recorded in the run fingerprint. It is not a secret. It only has to keep the
  label from being computable by the reader and from carrying the dataset's
  prefixes; a constant key (not a per-run random one) is what keeps
  ``context_sha256`` comparable between arms and runs, so paired comparison
  and ``compare_runs.py`` work unchanged and the mapping can be recomputed
  offline.
* Labels are scoped by question id, so one session id gets a different label
  under every question.
* Two different session ids that share a label inside one question raise
  :class:`SessionLabelCollisionError`. Nothing is ever silently merged.

Where raw ids may live: in memory (this module's :class:`SessionLabeler`) and
in a sidecar mapping file written next to the checkpoint
(:class:`SessionLabelSidecar`). Never in the store, never in the checkpoint
rows, never in the text the reader sees. A flag selects raw labels for
reproducing old runs; it is off by default.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading

SESSION_LABEL_MODE_RAW = "raw"
SESSION_LABEL_MODE_ANONYMISED = "hmac-v1"
SESSION_LABEL_MODES = (SESSION_LABEL_MODE_ANONYMISED, SESSION_LABEL_MODE_RAW)
# New runs hide the ids; raw labels are the opt-in for reproducing old runs.
DEFAULT_SESSION_LABEL_MODE = SESSION_LABEL_MODE_ANONYMISED

ANON_KEY_ID = "lme-anon-v1"
# Public on purpose (see the module docstring). Changing these bytes changes
# every label, so the key id above must change with them.
_ANON_KEYS: dict[str, bytes] = {
    ANON_KEY_ID: b"alicememory/longmemeval/session-label/lme-anon-v1",
}
ANON_LABEL_HEX_CHARS = 10
ANON_LABEL_PREFIX = "S"
ANON_LABEL_PATTERN = re.compile(r"^S[0-9a-f]{10}$")

SIDECAR_SCHEMA = "longmemeval_session_labels_v1"
SIDECAR_SUFFIX = ".session-labels.jsonl"


class SessionLabelError(ValueError):
    """A session label would be wrong, ambiguous, or would expose a raw id."""


class SessionLabelCollisionError(SessionLabelError):
    """Two different session ids in one question map to the same label."""


def anon_session_label(
    question_id: str,
    session_id: str,
    *,
    key_id: str = ANON_KEY_ID,
    hex_chars: int = ANON_LABEL_HEX_CHARS,
) -> str:
    """``"S"`` plus the first ``hex_chars`` hex digits of the keyed hash.

    ``hex_chars`` exists so a test can force a collision cheaply; real runs
    use the default.
    """
    key = _ANON_KEYS.get(key_id)
    if key is None:
        raise SessionLabelError(f"unknown session label key id {key_id!r} (known: {sorted(_ANON_KEYS)})")
    if not 1 <= hex_chars <= 64:
        raise SessionLabelError("hex_chars must be between 1 and 64")
    message = question_id.encode("utf-8") + b"\x00" + session_id.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return ANON_LABEL_PREFIX + digest[:hex_chars]


def key_id_for_mode(mode: str) -> str | None:
    """The key id a mode records in fingerprints and rows (``None`` for raw)."""
    validate_session_label_mode(mode)
    return ANON_KEY_ID if mode == SESSION_LABEL_MODE_ANONYMISED else None


def validate_session_label_mode(mode: str) -> str:
    if mode not in SESSION_LABEL_MODES:
        raise SessionLabelError(f"session label mode {mode!r} is not one of {SESSION_LABEL_MODES}")
    return mode


class SessionLabeler:
    """Per-question mapping between raw session ids and the labels shown.

    Holds the only in-memory copy of the raw ids. ``label_for`` is what every
    place that writes a session id into the store or renders one for the
    reader must call; ``raw_id`` exists for the sidecar file and offline tools
    and must never feed text the reader sees.
    """

    def __init__(
        self,
        question_id: str,
        session_ids: Iterable[str],
        *,
        mode: str = DEFAULT_SESSION_LABEL_MODE,
        hex_chars: int = ANON_LABEL_HEX_CHARS,
    ) -> None:
        self.question_id = question_id
        self.mode = validate_session_label_mode(mode)
        self.key_id = key_id_for_mode(mode)
        self._hex_chars = hex_chars
        self._raw_to_label: dict[str, str] = {}
        self._label_to_raw: dict[str, str] = {}
        for session_id in session_ids:
            self.label_for(session_id)

    def label_for(self, session_id: str) -> str:
        """The label for ``session_id``; fails loudly on a collision."""
        existing = self._raw_to_label.get(session_id)
        if existing is not None:
            return existing
        if self.mode == SESSION_LABEL_MODE_RAW:
            label = session_id
        else:
            label = anon_session_label(
                self.question_id,
                session_id,
                key_id=self.key_id or ANON_KEY_ID,
                hex_chars=self._hex_chars,
            )
        other = self._label_to_raw.get(label)
        if other is not None and other != session_id:
            raise SessionLabelCollisionError(
                f"question {self.question_id!r}: two different session ids map to the label {label!r}; "
                "refusing to merge them (the labels would make two sessions indistinguishable)"
            )
        self._raw_to_label[session_id] = label
        self._label_to_raw[label] = session_id
        return label

    def labels_for(self, session_ids: Iterable[str]) -> set[str]:
        return {self.label_for(session_id) for session_id in session_ids}

    def raw_id(self, label: str) -> str:
        """The raw id behind a label. For the sidecar and offline tools only."""
        try:
            return self._label_to_raw[label]
        except KeyError:
            raise SessionLabelError(f"{label!r} is not a label of question {self.question_id!r}") from None

    def is_known_label(self, label: str) -> bool:
        return label in self._label_to_raw

    def require_reader_safe(self, label: str) -> str:
        """Fence for ids read back from a store: fail loudly if it is not a label.

        A store ingested under another label mode (or by other code) would
        otherwise feed raw ids to the reader through the metadata read-back.
        Raw mode accepts anything because there is nothing to hide.
        """
        if self.mode == SESSION_LABEL_MODE_RAW:
            return label
        if not self.is_known_label(label) or not ANON_LABEL_PATTERN.match(label):
            raise SessionLabelError(
                f"question {self.question_id!r}: a stored source carries a session id that is not an "
                "anonymised label of this question; the store was ingested under a different label "
                "mode or by different code, and reading it would show raw ids to the reader"
            )
        return label

    def label_map(self) -> dict[str, str]:
        """``label -> raw id`` for every session this labeler has seen."""
        return dict(sorted(self._label_to_raw.items()))


def session_labeler_for_question(
    question: object,
    *,
    mode: str = DEFAULT_SESSION_LABEL_MODE,
    hex_chars: int = ANON_LABEL_HEX_CHARS,
) -> SessionLabeler:
    """Labeler covering a question's haystack and evidence session ids."""
    question_id = str(getattr(question, "question_id"))
    session_ids = [
        *getattr(question, "haystack_session_ids"),
        *getattr(question, "answer_session_ids"),
    ]
    return SessionLabeler(question_id, session_ids, mode=mode, hex_chars=hex_chars)


def validate_dataset_labels(questions: Iterable[object], *, mode: str) -> None:
    """Fail before any money is spent if a label collides anywhere in a dataset."""
    for question in questions:
        session_labeler_for_question(question, mode=mode)


def sidecar_path_for(checkpoint_path: Path) -> Path:
    """The mapping file that sits next to a checkpoint."""
    return checkpoint_path.with_name(checkpoint_path.stem + SIDECAR_SUFFIX)


@dataclass(frozen=True, slots=True)
class SessionLabelSidecarRecord:
    question_id: str
    mode: str
    key_id: str | None
    labels: dict[str, str]  # label -> raw session id

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SIDECAR_SCHEMA,
                "question_id": self.question_id,
                "session_label_mode": self.mode,
                "session_label_key_id": self.key_id,
                "labels": self.labels,
            },
            ensure_ascii=True,
            sort_keys=True,
        )


class SessionLabelSidecar:
    """Append-only ``label -> raw id`` file, one line per question run.

    A question that is retried appends a second, identical line.

    The only on-disk place raw ids may appear. It sits next to the checkpoint
    and is evidence-adjacent, never loaded into a store. Labels are also
    recomputable offline from the constant key, so losing it loses nothing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, labeler: SessionLabeler) -> None:
        if labeler.mode == SESSION_LABEL_MODE_RAW:
            return  # labels are the raw ids; a mapping file would only duplicate them
        record = SessionLabelSidecarRecord(
            question_id=labeler.question_id,
            mode=labeler.mode,
            key_id=labeler.key_id,
            labels=labeler.label_map(),
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
                handle.flush()


__all__ = [
    "ANON_KEY_ID",
    "ANON_LABEL_HEX_CHARS",
    "ANON_LABEL_PATTERN",
    "DEFAULT_SESSION_LABEL_MODE",
    "SESSION_LABEL_MODES",
    "SESSION_LABEL_MODE_ANONYMISED",
    "SESSION_LABEL_MODE_RAW",
    "SIDECAR_SCHEMA",
    "SIDECAR_SUFFIX",
    "SessionLabelCollisionError",
    "SessionLabelError",
    "SessionLabelSidecar",
    "SessionLabelSidecarRecord",
    "SessionLabeler",
    "anon_session_label",
    "key_id_for_mode",
    "session_labeler_for_question",
    "sidecar_path_for",
    "validate_dataset_labels",
    "validate_session_label_mode",
]
