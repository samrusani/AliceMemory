"""Vectors for the chunks of sources: plan, send, store.

``source_embedding_policy`` says what may be sent. This module does the work, in three steps that
never overlap in time with a database lock held:

1. **Plan.** A short read of the sources asked for. Each chunk gets one outcome: skipped (with the
   reason), already current, or to be sent (it has no vector, or its vector is stale). The text to
   send is built and hashed here.
2. **Send.** The texts go to the embeddings endpoint in batches, with no connection open. Just before
   each batch a short read judges the batch again: a chunk whose source was deleted, replaced, scrubbed
   or relabelled above what the endpoint may receive, or whose text changed, since the plan is dropped
   from the batch and left pending. A change committed after that read, while the request is in
   flight, can still reach the endpoint; the compare and set below keeps its vector out of the vault.
   The provider, the batch size, the timeout, the 400/413/422 split of a refused batch, the redirect
   rule and the loopback rule are those of the memory path (``vnext_embeddings``).
3. **Store.** A short write. For each vector the source and the text are read again and the vector is
   stored only when the source is still live and the text that would be sent now has the hash of the
   text that was sent (a compare and set). A source deleted, replaced or scrubbed while its chunks
   were being embedded, or a title or text that changed, leaves no vector.

The work is best effort. A failed batch leaves its chunks pending, nothing raises, and the receipt
counts what was embedded, what is pending (and why) and what was skipped (and why).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from alicebot_api.source_embedding_policy import (
    OFF_DRY_RUN,
    ChunkSignature,
    EgressPosture,
    SourceEmbeddingSettings,
    chunk_embedding_body,
    chunk_heading_paths,
    chunk_signature,
    document_prefix,
    egress_posture,
    endpoint_is_loopback,
    signature_of_row,
    skip_reason,
    source_embedding_settings,
    text_sha256,
)
from alicebot_api.vnext_stores.sqlite import source_chunk_vectors as vectors
from alicebot_api.vnext_embeddings import (
    MAX_EMBEDDINGS_BATCH_SIZE,
    EmbeddingProvider,
    EmbeddingTextFailure,
    cut_embedding_text,
    embed_batch_isolating,
    embedding_input_cap,
    get_embedding_provider,
    native_embedding_vector,
)

if TYPE_CHECKING:
    from alicebot_api.sqlite_store import SQLiteVNextStore

logger = logging.getLogger(__name__)

#: Sources read for one pass of plan, send and store. Small sources share a request.
SOURCES_PER_PASS = 20

PENDING_ENDPOINT_FAILED = "endpoint_failed"
PENDING_VAULT_CAP = "vault_cap"
PENDING_CHANGED = "changed_while_embedding"
PENDING_TIME_BUDGET = "time_budget"
PENDING_STORAGE_FAILED = "storage_failed"

VAULT_CAP_MESSAGE = (
    "the vault holds as many chunk vectors as ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS allows; "
    "raise the cap or delete sources, then run 'alice-memory reindex-embeddings --sources'"
)

StoreContext = Callable[[], AbstractContextManager["SQLiteVNextStore"]]


@dataclass(slots=True)
class ChunkEmbeddingReceipt:
    """What a pass did. ``reason`` is set when nothing could be sent at all."""

    reason: str | None = None
    sent: int = 0
    embedded: int = 0
    already_current: int = 0
    pending: int = 0
    pending_reasons: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    failure_reasons: Counter[str] = field(default_factory=Counter)

    def add_pending(self, reason: str, count: int = 1) -> None:
        if count > 0:
            self.pending += count
            self.pending_reasons[reason] += count

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "enabled": True,
            "sent": self.sent,
            "embedded": self.embedded,
            "pending": self.pending,
            "skipped": dict(sorted(self.skipped.items())),
            "skipped_total": sum(self.skipped.values()),
        }
        if self.already_current:
            record["already_current"] = self.already_current
        if self.pending_reasons:
            record["pending_reasons"] = dict(sorted(self.pending_reasons.items()))
        if PENDING_VAULT_CAP in self.pending_reasons:
            record["vault_cap_message"] = VAULT_CAP_MESSAGE
        if self.failure_reasons:
            record["failure_reasons"] = [
                {"reason": reason, "count": count}
                for reason, count in sorted(self.failure_reasons.items(), key=lambda item: (-item[1], item[0]))[:3]
            ]
        if self.reason is not None:
            record["reason"] = self.reason
        return record


@dataclass(frozen=True, slots=True)
class PlannedChunk:
    chunk_id: str
    source_id: str
    sent_text: str
    sha256: str
    replaces: bool


# -- one source, judged ----------------------------------------------------------------------------


@dataclass(slots=True)
class SourceOutcome:
    """The result of judging every chunk of one source."""

    skipped: Counter[str] = field(default_factory=Counter)
    current: int = 0
    stale: list[PlannedChunk] = field(default_factory=list)
    missing: list[PlannedChunk] = field(default_factory=list)

    @property
    def to_send(self) -> list[PlannedChunk]:
        return [*self.missing, *self.stale]


@dataclass(frozen=True, slots=True)
class ChunkText:
    chunk_id: str
    chunk_text: str
    body: str
    sent_text: str


def source_chunk_texts(
    source: Mapping[str, object],
    chunks: Sequence[Mapping[str, object]],
    model: str,
    input_cap: int | None = None,
) -> list[ChunkText]:
    """The text to send for every chunk of a source, in reading order.

    The header of a chunk is the title of the source and the headings above it, and those come from
    the chunks before it, so the chunks of one source are always read together. A text longer than the
    provider's input cap (``ALICE_EMBEDDINGS_MAX_INPUT_CHARS``, 8,000 by default, far above a chunk) is cut
    to it here, so the text that is hashed is the text that is sent, and a change of the cap makes the
    vectors of the chunks it cuts stale.
    """

    texts = [str(chunk.get("text") or "") for chunk in chunks]
    paths = chunk_heading_paths(texts)
    title = source.get("title")
    prefix = document_prefix(model)
    result: list[ChunkText] = []
    for chunk, text, path in zip(chunks, texts, paths, strict=True):
        body = chunk_embedding_body(
            title=title if isinstance(title, str) else None, heading_path=path, chunk_text=text
        )
        result.append(ChunkText(str(chunk["id"]), text, body, cut_embedding_text(prefix + body, input_cap)))
    return result


def judge_source(
    store: SQLiteVNextStore,
    source_id: str,
    *,
    posture: EgressPosture,
    model: str,
    signature: ChunkSignature | None,
    input_cap: int | None = None,
) -> SourceOutcome:
    """Judge every chunk of one source: skipped, current, missing a vector, or holding a stale one.

    ``signature`` is the signature of today's provider, or ``None`` when no provider is configured,
    and then no stored vector can be current.
    """

    outcome = SourceOutcome()
    source = vectors.embedding_source_row(store, source_id)
    if source is None:
        return outcome
    if not source["live"]:
        outcome.skipped["source_retired"] += vectors.count_embedding_source_chunks(store, source_id)
        return outcome
    held = vectors.chunk_vector_meta(store, source_id)
    for item in source_chunk_texts(source, vectors.embedding_source_chunks(store, source_id), model, input_cap):
        digest = text_sha256(item.sent_text)
        row = held.get(item.chunk_id)
        # A vector made with today's signature from today's text is current whatever the rules would say now:
        # the rules decide what is sent, and nothing is sent for it.
        if (
            row is not None
            and signature is not None
            and signature_of_row(row) == signature
            and row.get("content_sha256") == digest
        ):
            outcome.current += 1
            continue
        reason = skip_reason(
            chunk_text=item.chunk_text,
            sent_text_without_prefix=item.body,
            source_sensitivity=str(source.get("sensitivity") or "unknown"),
            source_live=True,
            posture=posture,
        )
        if reason is not None:
            outcome.skipped[reason] += 1
            continue
        planned = PlannedChunk(item.chunk_id, source_id, item.sent_text, digest, replaces=row is not None)
        (outcome.stale if row is not None else outcome.missing).append(planned)
    return outcome


# -- the run -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Egress:
    """The settings, the provider and the posture a run works under."""

    settings: SourceEmbeddingSettings
    provider: EmbeddingProvider | None
    posture: EgressPosture

    @property
    def signature(self) -> ChunkSignature | None:
        return chunk_signature(self.provider) if self.provider is not None else None

    @property
    def model(self) -> str:
        return str(getattr(self.provider, "model", ""))

    @property
    def input_cap(self) -> int | None:
        return embedding_input_cap(self.provider) if self.provider is not None else None


def resolve_egress(
    environ: Mapping[str, str] | None = None, *, provider: EmbeddingProvider | None = None
) -> Egress:
    """The settings and the provider of the environment. ``provider`` replaces the configured one (tests)."""

    settings = source_embedding_settings(environ)
    resolved = provider if provider is not None else get_embedding_provider()
    posture = egress_posture(
        settings,
        provider_configured=resolved is not None,
        base_url=str(getattr(resolved, "base_url", "")),
    )
    return Egress(settings, resolved, posture)


def _pages(source_ids: Sequence[str]) -> Iterator[list[str]]:
    unique = list(dict.fromkeys(str(item) for item in source_ids))
    for start in range(0, len(unique), SOURCES_PER_PASS):
        yield unique[start : start + SOURCES_PER_PASS]


def _plan_page(
    store: SQLiteVNextStore, source_ids: Sequence[str], egress: Egress, receipt: ChunkEmbeddingReceipt
) -> list[PlannedChunk]:
    """Judge the sources of one page, record the skips, and return the chunks to send within the cap."""

    to_send: list[PlannedChunk] = []
    for source_id in source_ids:
        outcome = judge_source(
            store,
            source_id,
            posture=egress.posture,
            model=egress.model,
            signature=egress.signature,
            input_cap=egress.input_cap,
        )
        receipt.skipped.update(outcome.skipped)
        receipt.already_current += outcome.current
        to_send.extend(outcome.to_send)
    room = max(0, egress.settings.max_vectors - vectors.count_chunk_vectors(store))
    kept: list[PlannedChunk] = []
    refused = 0
    for chunk in to_send:
        if chunk.replaces:
            kept.append(chunk)
        elif room > 0:
            room -= 1
            kept.append(chunk)
        else:
            refused += 1
    receipt.add_pending(PENDING_VAULT_CAP, refused)
    return kept


def _still_qualifying(
    store: SQLiteVNextStore, chunks: Sequence[PlannedChunk], egress: Egress
) -> list[PlannedChunk]:
    """The chunks of a batch that may be sent now, in their order.

    A chunk qualifies when its source is live, the text that would be sent has the hash it was planned with,
    and the egress policy lets it go as the source is labelled now (the credential check, the readable-text
    rules and, for a remote endpoint, the sensitivity ceiling). Everything the plan decided is decided again
    from the rows as they are now.
    """

    by_source: dict[str, list[PlannedChunk]] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source_id, []).append(chunk)
    keep: set[str] = set()
    for source_id, items in by_source.items():
        source = vectors.embedding_source_row(store, source_id)
        if source is None or not source["live"]:
            continue
        now = {
            text.chunk_id: text
            for text in source_chunk_texts(
                source, vectors.embedding_source_chunks(store, source_id), egress.model, egress.input_cap
            )
        }
        sensitivity = str(source.get("sensitivity") or "unknown")
        for chunk in items:
            current = now.get(chunk.chunk_id)
            if current is None or text_sha256(current.sent_text) != chunk.sha256:
                continue
            reason = skip_reason(
                chunk_text=current.chunk_text,
                sent_text_without_prefix=current.body,
                source_sensitivity=sensitivity,
                source_live=True,
                posture=egress.posture,
            )
            if reason is None:
                keep.add(chunk.chunk_id)
    return [chunk for chunk in chunks if chunk.chunk_id in keep]


def _send(
    chunks: Sequence[PlannedChunk],
    egress: Egress,
    receipt: ChunkEmbeddingReceipt,
    deadline: float | None,
    store_context: StoreContext,
) -> list[tuple[PlannedChunk, list[float]]]:
    """Send the texts in batches. A failed text stays pending.

    Each batch is judged again in a short store context that is closed before the request, so no lock is held
    while the endpoint is called. A chunk that no longer qualifies is counted as changed and is not sent.
    """

    provider = egress.provider
    if provider is None:
        return []
    prepared: list[tuple[PlannedChunk, list[float]]] = []
    for start in range(0, len(chunks), MAX_EMBEDDINGS_BATCH_SIZE):
        planned_batch = chunks[start : start + MAX_EMBEDDINGS_BATCH_SIZE]
        if deadline is not None and time.monotonic() >= deadline:
            receipt.add_pending(PENDING_TIME_BUDGET, len(chunks) - start)
            break
        try:
            with store_context() as store:
                batch = _still_qualifying(store, planned_batch, egress)
        except Exception as exc:  # noqa: BLE001 - best effort: a batch that cannot be judged is not sent
            logger.warning("source chunk embedding recheck failed: %s", type(exc).__name__)
            receipt.add_pending(PENDING_STORAGE_FAILED, len(planned_batch))
            receipt.failure_reasons[f"recheck: {type(exc).__name__}"] += len(planned_batch)
            continue
        receipt.add_pending(PENDING_CHANGED, len(planned_batch) - len(batch))
        if not batch:
            continue
        receipt.sent += len(batch)
        try:
            outcomes = embed_batch_isolating(provider, [chunk.sent_text for chunk in batch], native=True)
        except Exception as exc:  # noqa: BLE001 - best effort: any failure leaves the batch pending
            logger.warning("source chunk embedding batch failed: %s", type(exc).__name__)
            receipt.add_pending(PENDING_ENDPOINT_FAILED, len(batch))
            receipt.failure_reasons[type(exc).__name__] += len(batch)
            continue
        failed = 0
        for chunk, outcome in zip(batch, outcomes, strict=True):
            if isinstance(outcome, EmbeddingTextFailure):
                failed += 1
                receipt.failure_reasons[outcome.reason or "the embedding failed and the provider gave no reason"] += 1
                continue
            try:
                prepared.append((chunk, native_embedding_vector(outcome)))
            except Exception as exc:  # noqa: BLE001 - one malformed vector must not fail the batch
                failed += 1
                receipt.failure_reasons[type(exc).__name__] += 1
        if failed:
            receipt.add_pending(PENDING_ENDPOINT_FAILED, failed)
            logger.warning("source chunk embedding failed for %d of %d texts", failed, len(batch))
    return prepared


def _store_page(
    store: SQLiteVNextStore,
    prepared: Sequence[tuple[PlannedChunk, list[float]]],
    egress: Egress,
    receipt: ChunkEmbeddingReceipt,
) -> None:
    """Store the vectors whose source is still live and whose text is still the text that was sent.

    The counts are kept apart until the savepoint has been released, so a write that fails and rolls the page
    back is not counted as stored.
    """

    signature = egress.signature
    if egress.provider is None or signature is None:
        return
    by_source: dict[str, list[tuple[PlannedChunk, list[float]]]] = {}
    for item in prepared:
        by_source.setdefault(item[0].source_id, []).append(item)
    tally = ChunkEmbeddingReceipt()
    with store.savepoint(), vectors.secure_deletion(store):
        vectors.ensure_chunk_vector_table(store)
        stored_total = vectors.count_chunk_vectors(store)
        for source_id, items in by_source.items():
            source = vectors.embedding_source_row(store, source_id)
            if source is None or not source["live"]:
                tally.add_pending(PENDING_CHANGED, len(items))
                continue
            held = vectors.chunk_vector_meta(store, source_id)
            now = {
                text.chunk_id: text
                for text in source_chunk_texts(
                    source, vectors.embedding_source_chunks(store, source_id), egress.model, egress.input_cap
                )
            }
            for chunk, vector in items:
                current = now.get(chunk.chunk_id)
                if current is None or text_sha256(current.sent_text) != chunk.sha256:
                    tally.add_pending(PENDING_CHANGED)
                    continue
                replaces = chunk.chunk_id in held
                if not replaces and stored_total >= egress.settings.max_vectors:
                    tally.add_pending(PENDING_VAULT_CAP)
                    continue
                try:
                    with store.savepoint():
                        vectors.put_chunk_vector(
                            store,
                            chunk_id=chunk.chunk_id,
                            source_id=source_id,
                            vector=vector,
                            provider=signature.provider,
                            model=signature.model,
                            endpoint=signature.endpoint,
                            signature_version=signature.signature_version,
                            header_version=signature.header_version,
                            prefix_version=signature.prefix_version,
                            content_sha256=chunk.sha256,
                        )
                except sqlite3.IntegrityError:
                    # The chunk went away between the read and the write.
                    tally.add_pending(PENDING_CHANGED)
                    continue
                if not replaces:
                    stored_total += 1
                tally.embedded += 1
    receipt.embedded += tally.embedded
    for reason, count in tally.pending_reasons.items():
        receipt.add_pending(reason, count)


def run_source_embeddings(
    source_pages: Iterator[list[str]] | Sequence[list[str]],
    *,
    store_context: StoreContext,
    egress: Egress,
    deadline: float | None = None,
) -> ChunkEmbeddingReceipt:
    """Embed the chunks of the sources of each page. Never raises: a failure is counted and logged."""

    receipt = ChunkEmbeddingReceipt()
    if not egress.posture.may_send:
        receipt.reason = egress.posture.reason
        return receipt
    for page in source_pages:
        planned: list[PlannedChunk] = []
        try:
            with store_context() as store:
                planned = _plan_page(store, page, egress, receipt)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("source chunk embedding plan failed: %s", type(exc).__name__)
            receipt.failure_reasons[f"plan: {type(exc).__name__}"] += 1
            continue
        if not planned:
            continue
        prepared = _send(planned, egress, receipt, deadline, store_context)
        if not prepared:
            continue
        try:
            with store_context() as store:
                _store_page(store, prepared, egress, receipt)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("source chunk embedding storage failed: %s", type(exc).__name__)
            receipt.add_pending(PENDING_STORAGE_FAILED, len(prepared))
            receipt.failure_reasons[f"storage: {type(exc).__name__}"] += 1
    return receipt


def embed_new_sources(
    source_ids: Sequence[str],
    *,
    store_context: StoreContext,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    provider: EmbeddingProvider | None = None,
    time_budget_seconds: float | None = None,
) -> ChunkEmbeddingReceipt | None:
    """Embed the chunks of sources a door has just committed.

    Returns ``None`` when the opt-in is off: nothing is read, nothing is sent, and a door adds nothing to
    its receipt. With the opt-in on, a receipt always comes back, and ``reason`` says why nothing was sent
    when nothing was (no provider, a remote endpoint without its own opt-in, a dry run). Called after the
    door's transaction has committed, so no lock is held during a request. Never raises.
    """

    try:
        settings = source_embedding_settings(environ)
        if not settings.enabled:
            return None
        if dry_run:
            return ChunkEmbeddingReceipt(reason=OFF_DRY_RUN)
        if not source_ids:
            return ChunkEmbeddingReceipt()
        egress = resolve_egress(environ, provider=provider)
        deadline = None if time_budget_seconds is None else time.monotonic() + time_budget_seconds
        return run_source_embeddings(_pages(source_ids), store_context=store_context, egress=egress, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - capture and import never fail because of embeddings
        logger.warning("source chunk embedding failed: %s", type(exc).__name__)
        return ChunkEmbeddingReceipt(reason="embedding_failed")


def backfill_source_chunks(
    *,
    store_context: StoreContext,
    environ: Mapping[str, str] | None = None,
    provider: EmbeddingProvider | None = None,
) -> ChunkEmbeddingReceipt:
    """Embed every live chunk that has no current vector. The owner's command; it sends what the opt-in allows."""

    egress = resolve_egress(environ, provider=provider)

    def pages() -> Iterator[list[str]]:
        after: str | None = None
        while True:
            with store_context() as store:
                ids = vectors.live_source_ids_after(store, after, SOURCES_PER_PASS)
            if not ids:
                return
            after = ids[-1]
            yield ids

    return run_source_embeddings(pages(), store_context=store_context, egress=egress)


def drop_chunk_vectors(*, store_context: StoreContext) -> int:
    """Delete every chunk vector of the user, and the table when it is left empty. Returns how many rows went."""

    with store_context() as store:
        with store.savepoint():
            return int(vectors.drop_chunk_vector_table(store))


# -- the census ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class ChunkVectorCensus:
    """The state of the chunk vectors of a vault, for ``alice-memory doctor``."""

    current: int = 0
    pending: int = 0
    stale: int = 0
    stored: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


def census_egress(
    environ: Mapping[str, str] | None = None, *, provider: EmbeddingProvider | None = None
) -> Egress:
    """The egress a census judges by: what a pass would send if the opt-ins allowed it.

    ``resolve_egress`` answers "may anything be sent now"; a census answers "what would be sent", so a remote
    endpoint that the second opt-in has not allowed still carries the sensitivity ceiling here.
    """

    settings = source_embedding_settings(environ)
    resolved = provider if provider is not None else get_embedding_provider()
    remote = resolved is not None and not endpoint_is_loopback(str(getattr(resolved, "base_url", "")))
    posture = EgressPosture(
        may_send=True,
        reason=None,
        remote=remote,
        ceiling_rank=settings.max_sensitivity_rank if remote else None,
        ceiling=settings.max_sensitivity if remote else None,
    )
    return Egress(settings, resolved, posture)


def chunk_vector_census(store: SQLiteVNextStore, *, egress: Egress) -> ChunkVectorCensus:
    """Count the live chunks by state. Reads every live chunk; sends nothing.

    ``current`` has a vector made with today's signature from today's text. ``stale`` has a vector that is not
    that (another model, endpoint or rule version, or a title or text that has changed since). ``pending`` has no
    vector and may be sent. ``skipped`` is the chunks that are never sent, by reason. With no provider configured
    no vector can be current, so every stored vector counts as stale.
    """

    census = ChunkVectorCensus(stored=vectors.count_chunk_vectors(store))
    after: str | None = None
    while True:
        ids = vectors.live_source_ids_after(store, after, SOURCES_PER_PASS)
        if not ids:
            return census
        after = ids[-1]
        for source_id in ids:
            outcome = judge_source(
                store,
                source_id,
                posture=egress.posture,
                model=egress.model,
                signature=egress.signature,
                input_cap=egress.input_cap,
            )
            census.skipped.update(outcome.skipped)
            census.current += outcome.current
            census.stale += len(outcome.stale)
            census.pending += len(outcome.missing)


def _mode_line(egress: Egress) -> str:
    settings = egress.settings
    if not settings.enabled:
        return "source embeddings: off"
    if egress.provider is None:
        return "source embeddings: on, no embedding provider configured"
    if not egress.posture.remote:
        return "source embeddings: on, endpoint on this machine"
    if not settings.remote_allowed:
        return "source embeddings: on, remote endpoint not allowed (ALICE_SOURCE_EMBEDDINGS_REMOTE is not on)"
    return f"source embeddings: on, remote endpoint, sensitivity ceiling {settings.max_sensitivity}"


def chunk_vector_doctor_lines(store: SQLiteVNextStore, *, environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``alice-memory doctor`` lines for the vectors of source chunks.

    While the opt-in is off and the vault holds no chunk vector, nothing would be sent and nothing is stored,
    so the lines say so and the chunks are not read. Otherwise every live chunk is judged, as a pass would
    judge it, and counted: ``current``, ``pending`` (no vector, may be sent), ``stale`` (a vector that is
    another model's, another endpoint's, made under another rule, or made from text that has changed since)
    and ``skipped`` by reason.
    """

    egress = census_egress(environ)
    mode = _mode_line(egress)
    if not egress.settings.enabled and vectors.count_chunk_vectors(store) == 0:
        return [
            mode,
            "chunks with a current vector: 0",
            "chunks pending a vector: 0",
            "chunks with a stale vector: 0",
            "chunks skipped: not counted while source embeddings are off",
        ]
    census = chunk_vector_census(store, egress=egress)
    skipped_total = sum(census.skipped.values())
    skipped = f"{skipped_total}"
    if census.skipped:
        skipped += " (" + ", ".join(f"{reason} {count}" for reason, count in sorted(census.skipped.items())) + ")"
    note = " (no embedding provider configured)" if egress.provider is None else ""
    return [
        mode,
        f"chunks with a current vector: {census.current}",
        f"chunks pending a vector: {census.pending}",
        f"chunks with a stale vector: {census.stale}{note}",
        f"chunks skipped: {skipped}",
    ]


@contextmanager
def sqlite_store_context(db_path: str | Path, user_id: UUID | str) -> Iterator["SQLiteVNextStore"]:
    """A store in one short transaction on the vault at ``db_path``."""

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    with sqlite_user_connection(db_path, user_id) as conn:
        yield SQLiteVNextStore(conn, user_id)


__all__ = [
    "PENDING_CHANGED",
    "PENDING_ENDPOINT_FAILED",
    "PENDING_STORAGE_FAILED",
    "PENDING_TIME_BUDGET",
    "PENDING_VAULT_CAP",
    "SOURCES_PER_PASS",
    "VAULT_CAP_MESSAGE",
    "ChunkEmbeddingReceipt",
    "ChunkText",
    "ChunkVectorCensus",
    "Egress",
    "PlannedChunk",
    "SourceOutcome",
    "backfill_source_chunks",
    "census_egress",
    "chunk_vector_census",
    "chunk_vector_doctor_lines",
    "drop_chunk_vectors",
    "embed_new_sources",
    "judge_source",
    "resolve_egress",
    "run_source_embeddings",
    "source_chunk_texts",
    "sqlite_store_context",
]
