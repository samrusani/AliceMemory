"""The vector stage of passage recall: what the chunk vectors add to the lexical passages, and what they never may.

``ALICE_SEARCH_QUALITY=vectors`` runs the passage stage as it is and then lets the vectors of the chunks change
two things, and only two:

* **Which passage of a document is shown.** Inside every document the stage selects (one the words found, one only
  the vectors found, one a provenance or title list added), the document's chunks are ranked by reciprocal rank
  fusion of their lexical rank and their vector rank *within that document*, and the document's slots are filled
  in that order. The number of slots is what passage mode gave the document.
* **Which documents are shown.** A reserved share of the head of the result is given to documents that only the
  vector list found, best chunk first. The share is a third of the head (2 of 6 slots at the default limit of 8).

Everything else is passage mode's, and when the vectors have nothing to add the result is passage mode's, byte for
byte. The strict-then-OR decision of the lexical query reads the lexical list only; this module is not asked.

Safety, in the order a recall meets it:

* **Egress.** The query is sent to the embeddings endpoint, so the same opt-ins that govern the chunks govern the
  query: ``ALICE_SOURCE_EMBEDDINGS=on``, and for an endpoint that is not on this machine
  ``ALICE_SOURCE_EMBEDDINGS_REMOTE=on``. A query the credential check flags is not sent.
* **Failure.** Every error is caught and logged, and the status says why. The query embedding has a short timeout
  (about two seconds) and a circuit breaker: after a failure the stage is skipped for a short time instead of waiting
  for the endpoint on every recall.
* **The mask.** The vectors of a source the caller may not read are never ranked, scored or counted. The readable
  sources come from ``readable_source_ids``, which shares its predicate with the two lexical source reads. Counts in
  the status are counts after the mask, and no chunk id is ever put in a status.
* **The fence after.** A chunk this module picks goes through the same ``_usable`` path as a lexical one: the parent
  is resolved and checked again, a duplicate collapses, and a passage the credential check flags is dropped.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alicebot_api.credential_floor import credential_verdict
from alicebot_api.source_embedding_policy import (
    OFF_NO_PROVIDER,
    OFF_REMOTE_NOT_ALLOWED,
    OFF_SOURCE_EMBEDDINGS,
    chunk_signature,
    egress_posture,
    query_prefix,
    source_embedding_settings,
)
from alicebot_api.vnext_embeddings import (
    OpenAICompatibleEmbeddingProvider,
    embed_texts_native,
    endpoint_fingerprint,
    native_embedding_vector,
)
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import (
    REFUSED_OVER_CAP,
    ChunkVectorCacheRefused,
    ChunkVectorMask,
    ChunkVectorView,
    ScoredChunk,
    chunk_vector_view,
)
from alicebot_api.vnext_stores.sqlite.source_chunk_vectors import chunk_rows_by_ids
from alicebot_api.vnext_stores.sqlite.source_readable import readable_source_ids

logger = logging.getLogger(__name__)

# Statuses. The edge adds two of its own (minimal depth, a store with no chunk vectors).
STATUS_ENABLED = "enabled"
STATUS_OFF = "disabled: source embeddings are off"
STATUS_NO_PROVIDER = "disabled: no embedding provider configured"
STATUS_REMOTE_NOT_ALLOWED = "disabled: remote endpoint not allowed"
STATUS_NO_STORE_SUPPORT = "disabled: store does not support chunk vectors"
STATUS_CACHE_OVER_CAP = "skipped: resident vector cache over its cap"
STATUS_CACHE_UNAVAILABLE = "skipped: no resident vector cache for this database"
STATUS_QUERY_FAILED = "skipped: query embedding failed"
STATUS_BREAKER_OPEN = "skipped: query embedding failed recently"
STATUS_QUERY_WITHHELD = "skipped: query not sent (credential material)"
STATUS_STALE = "none usable: stored vectors are from another model, endpoint or rule"
STATUS_NONE = "none embedded"
STATUS_ERROR = "skipped: vector stage error"
#: The one status a caller with limits is shown in place of every status that rows it may not read can decide.
STATUS_UNAVAILABLE = "vector search unavailable"
#: The statuses decided by the rows of the whole vault, not only the rows the caller may read: the cap on the resident
#: copy counts every vector of the user, the width of the copy is the commonest width of all of them, and an error may
#: come from any one. A caller with limits is told none of them apart.
VAULT_DEPENDENT_STATUSES = frozenset(
    {STATUS_CACHE_OVER_CAP, STATUS_CACHE_UNAVAILABLE, STATUS_NONE, STATUS_STALE, STATUS_ERROR}
)

#: Seconds the query embedding may take before the recall goes on without vectors.
QUERY_EMBEDDING_TIMEOUT_SECONDS = 2.0
#: Seconds the stage is skipped after a failed query embedding.
BREAKER_SECONDS = 30.0
#: Chunks the global scan keeps (``limit * 4``, never fewer than 32, never more than 2,048).
DISCOVERY_DEPTH_FLOOR = 32
DISCOVERY_DEPTH_CAP = 2048
DISCOVERY_DEPTH_MULTIPLIER = 4
#: The most documents whose chunks are ranked within the document in one recall.
MAX_RANKED_SOURCES = 32
#: The share of the head given to documents only the vectors found: one slot in this many.
DISCOVERY_HEAD_DIVISOR = 3
#: The constant of reciprocal rank fusion inside a document (the same as everywhere else in recall).
WITHIN_SOURCE_RRF_K = 60

_OFF_STATUS = {
    OFF_SOURCE_EMBEDDINGS: STATUS_OFF,
    OFF_NO_PROVIDER: STATUS_NO_PROVIDER,
    OFF_REMOTE_NOT_ALLOWED: STATUS_REMOTE_NOT_ALLOWED,
}


# -- the circuit breaker --------------------------------------------------------------------------------------------

_BREAKER_LOCK = threading.Lock()
_BREAKER_UNTIL: dict[str, float] = {}


def _breaker_key(provider: object) -> str:
    return f"{endpoint_fingerprint(getattr(provider, 'base_url', ''))}|{getattr(provider, 'model', '')}"


def breaker_is_open(provider: object, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _BREAKER_LOCK:
        until = _BREAKER_UNTIL.get(_breaker_key(provider))
        if until is None:
            return False
        if current >= until:
            del _BREAKER_UNTIL[_breaker_key(provider)]
            return False
        return True


def trip_breaker(provider: object, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _BREAKER_LOCK:
        _BREAKER_UNTIL[_breaker_key(provider)] = current + BREAKER_SECONDS


def reset_breaker() -> None:
    with _BREAKER_LOCK:
        _BREAKER_UNTIL.clear()


def _query_provider(provider: Any) -> Any:
    """The provider that embeds the query: the same endpoint and model with the short timeout."""

    if isinstance(provider, OpenAICompatibleEmbeddingProvider):
        return OpenAICompatibleEmbeddingProvider(
            base_url=provider.base_url,
            model=provider.model,
            api_key=provider.api_key,
            timeout_seconds=min(int(provider.timeout_seconds), int(QUERY_EMBEDDING_TIMEOUT_SECONDS)),
            max_input_chars=provider.max_input_chars,
        )
    return provider


# -- fusion inside a document -----------------------------------------------------------------------------------------


def fuse_within_source(
    lexical: Sequence[str],
    vector: Sequence[str],
    *,
    chunk_index: Mapping[str, int] | None = None,
    k: int = WITHIN_SOURCE_RRF_K,
) -> list[str]:
    """Order the chunks of one document by reciprocal rank fusion of their lexical rank and their vector rank.

    ``lexical`` and ``vector`` are chunk ids, best first. A chunk in one list scores ``1 / (k + rank)`` for it, a
    chunk in both scores the sum, and a chunk in neither is not a candidate. Equal scores fall to the chunk with the
    better lexical rank, then the better vector rank, then the lower chunk index, then the id, so the order is total
    and does not depend on the order the stores returned rows in.
    """

    lexical_rank = {chunk_id: rank for rank, chunk_id in enumerate(lexical, start=1)}
    vector_rank = {chunk_id: rank for rank, chunk_id in enumerate(vector, start=1)}
    candidates = list(dict.fromkeys([*lexical, *vector]))
    indexes = chunk_index or {}
    big = len(candidates) + len(lexical) + len(vector) + 1

    def key(chunk_id: str) -> tuple[float, int, int, int, str]:
        score = 0.0
        if chunk_id in lexical_rank:
            score += 1.0 / (k + lexical_rank[chunk_id])
        if chunk_id in vector_rank:
            score += 1.0 / (k + vector_rank[chunk_id])
        return (-score, lexical_rank.get(chunk_id, big), vector_rank.get(chunk_id, big), indexes.get(chunk_id, 0), chunk_id)

    return sorted(candidates, key=key)


def discovery_depth(limit: int) -> int:
    return min(max(limit * DISCOVERY_DEPTH_MULTIPLIER, DISCOVERY_DEPTH_FLOOR), DISCOVERY_DEPTH_CAP)


def reserved_discovery_slots(head_slots: int) -> int:
    """How many slots of a head of ``head_slots`` passages are held for documents only the vectors found."""

    return max(head_slots, 0) // DISCOVERY_HEAD_DIVISOR


# -- one recall's use of the vectors ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class VectorOutcome:
    """What the vectors say about one recall. Chunk rows are the stored rows; no id is ever put in a record."""

    #: source id -> chunk rows to show, in the order to show them, for a document the words found.
    picks: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    #: documents only the vectors found, best chunk first: (source id, chunk row).
    discovered: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    #: source id -> the chunk row that stands for a filler document.
    filler_picks: dict[str, dict[str, object]] = field(default_factory=dict)


class ChunkVectorSession:
    """One recall's view of the chunk vectors. Enter it, read ``status``, call ``compute`` once, leave it.

    Nothing here raises. A failure sets ``status`` and leaves ``compute`` returning ``None``, and the caller goes on
    as passage mode.
    """

    def __init__(
        self,
        *,
        store: Any,
        provider: Any,
        query: str,
        limit: int,
        domains: list[str] | None,
        sensitivity_allowed: list[str] | None,
        scope_projects: tuple[str, ...] = (),
        scope_people: tuple[str, ...] = (),
        scope_window_start: datetime | None = None,
        scope_window_end: datetime | None = None,
    ) -> None:
        self.status = STATUS_ENABLED
        self.readable_chunks = 0
        self.stale_chunks = 0
        self._store = store
        self._provider = provider
        self._query = query
        self._limit = limit
        self._domains = domains
        self._sensitivity_allowed = sensitivity_allowed
        self._scope_projects = scope_projects
        self._scope_people = scope_people
        self._scope_window_start = scope_window_start
        self._scope_window_end = scope_window_end
        self._stack = ExitStack()
        self._view: ChunkVectorView | None = None
        self._mask: ChunkVectorMask | None = None
        self._query_vector: list[float] | None = None
        self._depth = discovery_depth(limit)

    @property
    def enabled(self) -> bool:
        return self.status == STATUS_ENABLED and self._view is not None and self._mask is not None

    def __enter__(self) -> "ChunkVectorSession":
        try:
            self._open()
        except ChunkVectorCacheRefused as exc:
            self._close_view()
            self.status = STATUS_CACHE_OVER_CAP if exc.reason == REFUSED_OVER_CAP else STATUS_CACHE_UNAVAILABLE
        except Exception as exc:  # noqa: BLE001 - the vector stage never fails a recall
            logger.warning("chunk vector stage failed error_type=%s", type(exc).__name__)
            self._close_view()
            self.status = STATUS_ERROR
        return self

    def __exit__(self, *_exc: object) -> None:
        self._close_view()

    def _close_view(self) -> None:
        self._view = None
        self._mask = None
        self._stack.close()
        self._stack = ExitStack()

    def _open(self) -> None:
        store = self._store
        provider = self._provider
        if getattr(store, "conn", None) is None or type(store).__name__ != "SQLiteVNextStore":
            self.status = STATUS_NO_STORE_SUPPORT
            return
        settings = source_embedding_settings()
        posture = egress_posture(
            settings, provider_configured=provider is not None, base_url=str(getattr(provider, "base_url", ""))
        )
        if not posture.may_send:
            self.status = _OFF_STATUS.get(posture.reason or "", STATUS_OFF)
            return
        if breaker_is_open(provider):
            self.status = STATUS_BREAKER_OPEN
            return
        if credential_verdict(self._query) is not None:
            self.status = STATUS_QUERY_WITHHELD
            return
        # The request first, with no lock held and before the cache is touched: a slow endpoint stalls this recall
        # for at most the short timeout, and never any other reader of the cache.
        try:
            text = query_prefix(str(getattr(provider, "model", ""))) + self._query
            vectors = embed_texts_native(_query_provider(provider), [text])
            self._query_vector = native_embedding_vector(vectors[0])
        except Exception as exc:  # noqa: BLE001 - any failure of the endpoint
            logger.warning("chunk query embedding failed error_type=%s", type(exc).__name__)
            trip_breaker(provider)
            self.status = STATUS_QUERY_FAILED
            return
        readable = readable_source_ids(
            store,
            domains=self._domains,
            sensitivity_allowed=self._sensitivity_allowed,
            scope_projects=self._scope_projects,
            scope_people=self._scope_people,
            scope_window_start=self._scope_window_start,
            scope_window_end=self._scope_window_end,
            with_vectors=True,
        )
        view = self._stack.enter_context(chunk_vector_view(store.conn, store.user_id, chunk_signature(provider)))
        mask = view.mask(readable)
        self._view = view
        self._mask = mask
        self.readable_chunks = mask.rows
        self.stale_chunks = mask.stale
        if view.dims and len(self._query_vector) != view.dims and mask.rows:
            # The model now answers at another width than the vectors were made at: they cannot be compared.
            self.status = STATUS_STALE
        elif mask.rows == 0:
            self.status = STATUS_STALE if mask.stale else STATUS_NONE

    def compute(
        self,
        *,
        lexical_hits: Mapping[str, Sequence[Mapping[str, object]]],
        slots: Mapping[str, int],
        filler_ids: Sequence[str],
    ) -> VectorOutcome | None:
        """Everything the vectors add to this recall, computed before any of it is used.

        ``lexical_hits`` maps each document the words found to its lexical chunk rows, best first; ``slots`` is the
        number of passages passage mode gave it; ``filler_ids`` are documents a provenance, title or time list
        added. Returns ``None`` when the stage is not enabled or anything fails, and then nothing has changed.
        """

        if not self.enabled or self._view is None or self._mask is None or self._query_vector is None:
            return None
        view, mask, query = self._view, self._mask, self._query_vector
        try:
            top = view.top_chunks(mask, query, self._depth)
            known: dict[str, Mapping[str, object]] = {}
            for hit_rows in lexical_hits.values():
                for hit in hit_rows:
                    known[str(hit["id"])] = hit
            ranked_ids: dict[str, list[str]] = {}
            ranked_sources = list(dict.fromkeys([*lexical_hits.keys()]))[:MAX_RANKED_SOURCES]
            for source_id in ranked_sources:
                ranked = view.rank_in_source(mask, source_id, query)
                ranked_ids[source_id] = [item.chunk_id for item in ranked]
            lexical_sources = set(lexical_hits.keys())
            discovered_order: list[tuple[str, str]] = []
            seen: set[str] = set()
            for item in top:
                if item.source_id in lexical_sources or item.source_id in seen:
                    continue
                seen.add(item.source_id)
                discovered_order.append((item.source_id, item.chunk_id))
                if len(discovered_order) >= max(self._limit, 1):
                    break
            filler_ranked: dict[str, str] = {}
            for source_id in list(dict.fromkeys(filler_ids))[:MAX_RANKED_SOURCES]:
                if source_id in lexical_sources or source_id in seen:
                    continue
                ranked = view.rank_in_source(mask, source_id, query)
                if ranked:
                    filler_ranked[source_id] = ranked[0].chunk_id
            wanted: dict[str, list[str]] = {}
            for source_id in ranked_sources:
                fused = fuse_within_source([str(row["id"]) for row in lexical_hits[source_id]], ranked_ids[source_id])
                wanted[source_id] = fused[: max(int(slots.get(source_id, 0)), 0)]
            need = {chunk_id for ids in wanted.values() for chunk_id in ids if chunk_id not in known}
            need.update(chunk_id for _source, chunk_id in discovered_order)
            need.update(filler_ranked.values())
            fetched = chunk_rows_by_ids(self._store, sorted(need - set(known)))
            rows: dict[str, dict[str, object]] = {key: dict(value) for key, value in known.items()}
            rows.update(fetched)
            outcome = VectorOutcome()
            for source_id, chunk_ids in wanted.items():
                outcome.picks[source_id] = [rows[chunk_id] for chunk_id in chunk_ids if chunk_id in rows]
            for source_id, chunk_id in discovered_order:
                if chunk_id in rows and str(rows[chunk_id].get("source_id")) == source_id:
                    outcome.discovered.append((source_id, rows[chunk_id]))
            for source_id, chunk_id in filler_ranked.items():
                if chunk_id in rows and str(rows[chunk_id].get("source_id")) == source_id:
                    outcome.filler_picks[source_id] = rows[chunk_id]
            return outcome
        except Exception as exc:  # noqa: BLE001 - the vector stage never fails a recall
            logger.warning("chunk vector ranking failed error_type=%s", type(exc).__name__)
            self.status = STATUS_ERROR
            return None

    def record(self, *, discovered_in_head: int = 0, reordered_slots: int = 0, filler_picks: int = 0) -> dict[str, object]:
        """The part of the debug record the vectors own: a status and counts of what was readable and what changed.

        Every count is of a row the caller may read (after the mask) or of an entry returned to the caller. No id.
        """

        return {
            "status": self.status,
            "readable_chunks": self.readable_chunks,
            "stale_chunks": self.stale_chunks,
            "discovered_in_head": discovered_in_head,
            "reordered_slots": reordered_slots,
            "filler_picks": filler_picks,
        }


def limited_record(record: Mapping[str, object]) -> dict[str, object]:
    """The vector part of a debug record as a caller with limits is shown it.

    A status that rows the caller may not read can decide (``VAULT_DEPENDENT_STATUSES``) becomes one status with no
    counts, so two vaults that differ only by a vector the caller may not read give the same bytes on either side of
    the cap. A status decided by the configuration, the query or the endpoint, and ``enabled`` with counts of rows the
    caller may read and entries it was returned, are left as they are. The owner and an unbound admin key are not limited and
    are shown the record as the stage made it.
    """

    if record.get("status") in VAULT_DEPENDENT_STATUSES:
        return disabled_record(STATUS_UNAVAILABLE)
    return dict(record)


def disabled_record(status: str) -> dict[str, object]:
    """The vector part of a debug record when the stage did not run at all."""

    return {
        "status": status,
        "readable_chunks": 0,
        "stale_chunks": 0,
        "discovered_in_head": 0,
        "reordered_slots": 0,
        "filler_picks": 0,
    }


__all__ = [
    "BREAKER_SECONDS",
    "ChunkVectorSession",
    "DISCOVERY_DEPTH_CAP",
    "DISCOVERY_DEPTH_FLOOR",
    "DISCOVERY_HEAD_DIVISOR",
    "MAX_RANKED_SOURCES",
    "QUERY_EMBEDDING_TIMEOUT_SECONDS",
    "STATUS_BREAKER_OPEN",
    "STATUS_CACHE_OVER_CAP",
    "STATUS_CACHE_UNAVAILABLE",
    "STATUS_ENABLED",
    "STATUS_ERROR",
    "STATUS_NONE",
    "STATUS_NO_PROVIDER",
    "STATUS_NO_STORE_SUPPORT",
    "STATUS_OFF",
    "STATUS_QUERY_FAILED",
    "STATUS_QUERY_WITHHELD",
    "STATUS_REMOTE_NOT_ALLOWED",
    "STATUS_STALE",
    "STATUS_UNAVAILABLE",
    "ScoredChunk",
    "VAULT_DEPENDENT_STATUSES",
    "VectorOutcome",
    "WITHIN_SOURCE_RRF_K",
    "breaker_is_open",
    "disabled_record",
    "discovery_depth",
    "fuse_within_source",
    "limited_record",
    "reserved_discovery_slots",
    "reset_breaker",
    "trip_breaker",
]
