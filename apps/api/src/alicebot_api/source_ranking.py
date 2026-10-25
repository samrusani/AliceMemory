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
    source id may repeat. ``alice_recall`` runs it by default on the SQLite store
    and when ``ALICE_SEARCH_QUALITY=passage``. ``vectors`` is passage mode plus the
    vectors of the chunks (SQLite only): which passage of a document is shown is
    chosen with the chunk vectors as well as the words, and a document only the
    vectors found can take a share of the head. It is its own mode, recorded as
    ``vectors`` wherever a mode is recorded, and it answers exactly as ``passage``
    does whenever the vector stage has nothing to add. The pack, the brief and every
    other caller stay on ``document``.

``passages_per_source``
    The most passages one document may contribute in ``passage`` mode. Document
    mode shows one chunk per document, so its value there is 1.

``scoped_vectors``
    Only read in ``vectors`` mode. True for a caller whose key has limits: the vector stage then reads a copy of the
    chunk vectors built from the sources that caller may read, so that whether the stage runs, what it reports and
    what it ranks are functions of rows the caller may read. False for the owner and an unbound admin key, who are
    served from the copy of every vector of the user. The edge sets it (``recall_source_ranking``); no library code does.

``lexical``
    ``legacy`` is today's strict-then-fallback chunk query. ``improved`` is the
    long-question change of the lexical slice. Nothing builds or runs it yet.

Why a required argument
-----------------------

A defaulted ``ranking`` would be a silent choice for whoever forgets it. The
retrieval service therefore takes it with no default, and a caller writes
``SourceRanking.document()`` where it means the ranking v0.20.0 has. A passage
value with the legacy lexical query is the passage stage. ``lexical="improved"``
and ``ALICE_SEARCH_QUALITY=on`` stay refused until that slice is built.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_args

SourceRankingMode = Literal["document", "passage", "vectors"]
SourceLexicalMode = Literal["legacy", "improved"]

SOURCE_RANKING_MODES: Final[tuple[str, ...]] = get_args(SourceRankingMode)
SOURCE_LEXICAL_MODES: Final[tuple[str, ...]] = get_args(SourceLexicalMode)

#: Document mode shows one chunk per document.
DOCUMENT_PASSAGES_PER_SOURCE: Final[int] = 1

#: The most passages one document contributes in passage mode.
#: The maintainers' cap rule changes this one line. Ties in that rule prefer 3.
PASSAGES_PER_SOURCE: Final[int] = 3

#: ``off`` is document mode, ``passage`` is the passage stage, ``vectors`` is the passage stage with the
#: chunk vectors, ``on`` stays refused.
SEARCH_QUALITY_ENV: Final[str] = "ALICE_SEARCH_QUALITY"
_SEARCH_QUALITY_VALUES: Final[tuple[str, ...]] = ("off", "passage", "vectors", "on")


def search_quality_requests_vectors(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``ALICE_SEARCH_QUALITY`` asks for ``vectors``, whatever store reads it."""

    raw = (os.environ if environ is None else environ).get(SEARCH_QUALITY_ENV)
    return raw is not None and raw.strip().casefold() == "vectors"


@dataclass(frozen=True, slots=True)
class SourceRanking:
    """The ranking one source read uses. Build it with ``SourceRanking.document()``."""

    mode: SourceRankingMode
    passages_per_source: int
    lexical: SourceLexicalMode
    scoped_vectors: bool = False

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

    @classmethod
    def passage(cls) -> SourceRanking:
        """Passage mode at the cap in ``PASSAGES_PER_SOURCE``, legacy lexical query."""

        return cls(mode="passage", passages_per_source=PASSAGES_PER_SOURCE, lexical="legacy")

    @classmethod
    def vectors(cls) -> SourceRanking:
        """Passage mode with the chunk vectors, at the same cap and on the legacy lexical query."""

        return cls(mode="vectors", passages_per_source=PASSAGES_PER_SOURCE, lexical="legacy")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, unset: SourceRanking | None = None
    ) -> SourceRanking:
        """The ranking ``ALICE_SEARCH_QUALITY`` names.

        ``off`` is document mode. ``passage`` is :meth:`passage`. ``vectors`` is
        :meth:`vectors`. ``on`` is the
        passage cap with the improved lexical query, which this build refuses
        at the stage. Any other value is refused here. An unset variable is
        ``unset``, which is document mode unless the caller passes another
        ranking: the MCP recall edge passes the default of the store it reads.
        Library code does not call this: the MCP recall edge does, then applies
        the PostgreSQL limited-key rule.
        """

        source = os.environ if environ is None else environ
        raw = source.get(SEARCH_QUALITY_ENV)
        if raw is None:
            return cls.document() if unset is None else unset
        value = raw.strip().casefold()
        if value == "off":
            return cls.document()
        if value == "passage":
            return cls.passage()
        if value == "vectors":
            return cls.vectors()
        if value == "on":
            return cls(mode="passage", passages_per_source=PASSAGES_PER_SOURCE, lexical="improved")
        raise ValueError(
            f"{SEARCH_QUALITY_ENV} must be one of {', '.join(_SEARCH_QUALITY_VALUES)}"
        )
