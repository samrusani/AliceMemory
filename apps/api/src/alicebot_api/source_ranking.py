"""How the source stage ranks what it finds (search-quality spec 4.2).

``SourceRanking`` is a small frozen value. It travels from the edge that decides to
the retrieval service as a required keyword-only argument named ``ranking``. Library
code never reads the environment to choose one: a service call that is handed
``SourceRanking.document()`` behaves the same whatever the process environment
holds, which is what lets the eval harness, the brief, the context pack and every
unit test stay on the ranking they were written against.

Fields
------

``mode``
    ``document`` is the ranking v0.20.0 has: sources are fused by document, each
    document shows the one chunk that won, and the result holds at most one entry
    per document. ``passage`` is the ranking of the passage-level slice: chunks
    rank on their own, several passages of one document may come back and the same
    source id may repeat. Nothing builds or runs ``passage`` yet.

``passages_per_source``
    The most passages one document may contribute in ``passage`` mode. Document
    mode shows one chunk per document, so its value there is 1.

``lexical``
    ``legacy`` is today's strict-then-fallback chunk query. ``improved`` is the
    long-question change of the lexical slice. Nothing builds or runs it yet.

Why a required argument
-----------------------

A defaulted ``ranking`` would be a silent choice for whoever forgets it. The
retrieval service therefore takes it with no default, and a caller writes
``SourceRanking.document()`` where it means the ranking v0.20.0 has. Until the
passage stage exists the service refuses every other value (see
``VNextRetrievalService._source_stage_lists``), so a value that no code path can
honour is never accepted and ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

SourceRankingMode = Literal["document", "passage"]
SourceLexicalMode = Literal["legacy", "improved"]

SOURCE_RANKING_MODES: Final[tuple[str, ...]] = get_args(SourceRankingMode)
SOURCE_LEXICAL_MODES: Final[tuple[str, ...]] = get_args(SourceLexicalMode)

#: Document mode shows one chunk per document.
DOCUMENT_PASSAGES_PER_SOURCE: Final[int] = 1


@dataclass(frozen=True, slots=True)
class SourceRanking:
    """The ranking one source read uses. Build it with ``SourceRanking.document()``."""

    mode: SourceRankingMode
    passages_per_source: int
    lexical: SourceLexicalMode

    def __post_init__(self) -> None:
        if self.mode not in SOURCE_RANKING_MODES:
            raise ValueError(f"source ranking mode must be one of {SOURCE_RANKING_MODES}, got {self.mode!r}")
        if self.lexical not in SOURCE_LEXICAL_MODES:
            raise ValueError(f"source ranking lexical must be one of {SOURCE_LEXICAL_MODES}, got {self.lexical!r}")
        if (
            isinstance(self.passages_per_source, bool)
            or not isinstance(self.passages_per_source, int)
            or self.passages_per_source < 1
        ):
            raise ValueError(
                f"source ranking passages_per_source must be a positive integer, got {self.passages_per_source!r}"
            )

    @classmethod
    def document(cls) -> SourceRanking:
        """The ranking v0.20.0 has. The value of every caller that is not ``alice_recall``."""

        return cls(mode="document", passages_per_source=DOCUMENT_PASSAGES_PER_SOURCE, lexical="legacy")
