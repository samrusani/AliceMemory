"""Precomputed date arithmetic: pure functions + the pack-level derived block.

Covers ``vnext_temporal_query``'s delta/duration/ordinal/timeline helpers
(calendar edges: leap days, month-end clamping, non-UTC offsets, naive
inputs) and the ``temporal precompute`` marked block in
``compile_context_pack``: ISO-8601 ``event_time`` stamps on selected items,
the bounded ``[derived]`` derived-values block, the
``temporal_precompute`` trace stage, reference_time-absent dormancy, and
determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from alicebot_api.vnext_retrieval import (
    CONTEXT_DEPTH_MINIMAL,
    VNextRetrievalRequest,
    VNextRetrievalService,
)
from alicebot_api.vnext_temporal_query import (
    DERIVED_LINE_MARKER,
    DERIVED_TIMELINE_MAX_LINES,
    delta_to_reference,
    derived_timeline_lines,
    duration_between,
    iso_day_with_weekday,
    ordinal_position,
)
from alicebot_api.vnext_source_fence import SourceReadFence


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_ambient_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_RERANKER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# -- delta_to_reference ----------------------------------------------------------


def test_delta_matches_design_example() -> None:
    # The design's canonical machine form: humanized delta, then the
    # chronological arrow pair.
    assert delta_to_reference(_utc(2023, 5, 30), _utc(2024, 1, 3)) == (
        "218 days (7 months 4 days) earlier; 2023-05-30 -> 2024-01-03"
    )


def test_delta_direction_words_and_chronological_arrow() -> None:
    # Later events keep the arrow chronological (earlier date first).
    assert delta_to_reference(_utc(2024, 1, 8), _utc(2024, 1, 3)) == (
        "5 days later; 2024-01-03 -> 2024-01-08"
    )
    assert delta_to_reference(_utc(2024, 1, 2), _utc(2024, 1, 3)) == (
        "1 day earlier; 2024-01-02 -> 2024-01-03"
    )


def test_delta_same_utc_day_ignores_clock_time() -> None:
    assert delta_to_reference(_utc(2024, 1, 3, 23, 59), _utc(2024, 1, 3)) == "same day (2024-01-03)"


def test_delta_weeks_breakdown_between_seven_and_sixty_days() -> None:
    assert delta_to_reference(_utc(2023, 12, 11), _utc(2024, 1, 3)) == (
        "23 days (3 weeks 2 days) earlier; 2023-12-11 -> 2024-01-03"
    )
    # Exact weeks omit the day remainder.
    assert delta_to_reference(_utc(2023, 6, 1), _utc(2023, 6, 15)) == (
        "14 days (2 weeks) earlier; 2023-06-01 -> 2023-06-15"
    )
    # Under a week there is nothing to break down.
    assert delta_to_reference(_utc(2023, 6, 12), _utc(2023, 6, 15)) == (
        "3 days earlier; 2023-06-12 -> 2023-06-15"
    )


def test_delta_calendar_breakdown_handles_leap_days() -> None:
    # Leap day to the following Feb 28: exactly 12 clamped months.
    assert delta_to_reference(_utc(2024, 2, 29), _utc(2025, 2, 28)) == (
        "365 days (1 year) earlier; 2024-02-29 -> 2025-02-28"
    )
    # Feb 28 to the next year's leap day: one extra remainder day.
    assert delta_to_reference(_utc(2023, 2, 28), _utc(2024, 2, 29)) == (
        "366 days (1 year 1 day) earlier; 2023-02-28 -> 2024-02-29"
    )


def test_delta_month_end_clamping() -> None:
    # Jan 31 + 1 month clamps to Feb 29 (leap 2024); remainder counts from there.
    assert delta_to_reference(_utc(2024, 1, 31), _utc(2024, 3, 31)) == (
        "60 days (2 months) earlier; 2024-01-31 -> 2024-03-31"
    )


def test_delta_is_timezone_honest_and_treats_naive_as_utc() -> None:
    # 00:30 at +02:00 is 22:30 the previous UTC day — a real DST-style edge.
    offset_event = datetime(2023, 3, 26, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert delta_to_reference(offset_event, _utc(2023, 3, 26)) == (
        "1 day earlier; 2023-03-25 -> 2023-03-26"
    )
    naive = datetime(2023, 5, 30, 8, 0)
    assert delta_to_reference(naive, _utc(2023, 6, 15)) == delta_to_reference(
        _utc(2023, 5, 30, 8, 0), _utc(2023, 6, 15)
    )
    # Naive reference too.
    assert delta_to_reference(_utc(2023, 5, 30), datetime(2023, 6, 15, 10, 0)) == (
        "16 days (2 weeks 2 days) earlier; 2023-05-30 -> 2023-06-15"
    )


# -- duration_between / ordinal_position / iso_day_with_weekday -------------------


def test_duration_between_is_order_insensitive() -> None:
    expected = "159 days (5 months 8 days); 2023-01-02 -> 2023-06-10"
    assert duration_between(_utc(2023, 1, 2), _utc(2023, 6, 10)) == expected
    assert duration_between(_utc(2023, 6, 10), _utc(2023, 1, 2)) == expected
    assert duration_between(_utc(2023, 6, 10, 1), _utc(2023, 6, 10, 23)) == "same day (2023-06-10)"


def test_ordinal_position_over_distinct_utc_days() -> None:
    series = [
        _utc(2023, 5, 30, 14),
        _utc(2023, 1, 2),
        _utc(2023, 5, 30, 2),  # same day as the first: collapses
        _utc(2023, 6, 10),
    ]
    assert ordinal_position(_utc(2023, 5, 30, 9), series) == (2, 3)
    assert ordinal_position(_utc(2023, 1, 2, 23, 59), series) == (1, 3)
    assert ordinal_position(_utc(2023, 6, 10), series) == (3, 3)
    with pytest.raises(ValueError, match="not in the series"):
        ordinal_position(_utc(2023, 2, 14), series)


def test_iso_day_with_weekday() -> None:
    assert iso_day_with_weekday(_utc(2023, 5, 30, 2, 1)) == "2023-05-30 (Tue)"
    # Offset conversion can shift the weekday too.
    late_offset = datetime(2023, 5, 30, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert iso_day_with_weekday(late_offset) == "2023-05-29 (Mon)"


# -- derived_timeline_lines --------------------------------------------------------


def test_derived_timeline_lines_shape_and_marker() -> None:
    events = [
        _utc(2023, 5, 30, 14),
        _utc(2023, 5, 20, 7),
        _utc(2023, 5, 30, 2),  # same-day duplicate collapses
        _utc(2023, 6, 2),
    ]
    lines = derived_timeline_lines(events, reference_time=_utc(2023, 6, 15, 10))
    assert lines == [
        "[derived] reference date: 2023-06-15 (Thu)",
        "[derived] dated items span 13 days (1 week 6 days); 2023-05-20 -> 2023-06-02",
        "[derived] 2023-05-20 (Sat): 26 days (3 weeks 5 days) earlier; 2023-05-20 -> 2023-06-15; day 1 of 3",
        "[derived] 2023-05-30 (Tue): 16 days (2 weeks 2 days) earlier; 2023-05-30 -> 2023-06-15; day 2 of 3",
        "[derived] 2023-06-02 (Fri): 13 days (1 week 6 days) earlier; 2023-06-02 -> 2023-06-15; day 3 of 3",
    ]
    assert all(line.startswith(DERIVED_LINE_MARKER + " ") for line in lines)


def test_derived_timeline_lines_dormant_without_events() -> None:
    assert derived_timeline_lines([], reference_time=_utc(2023, 6, 15)) == []


def test_derived_timeline_single_event_has_no_span_line() -> None:
    lines = derived_timeline_lines([_utc(2023, 5, 30)], reference_time=_utc(2023, 6, 15))
    assert len(lines) == 2
    assert "span" not in lines[0] and "span" not in lines[1]
    assert lines[1].endswith("day 1 of 1")


def test_derived_timeline_lines_bounded_and_relevance_kept() -> None:
    # 30 distinct days, relevance order = most relevant first.
    events = [_utc(2023, 1, 1) + timedelta(days=3 * index) for index in range(30)]
    lines = derived_timeline_lines(events, reference_time=_utc(2023, 6, 15))
    assert len(lines) == DERIVED_TIMELINE_MAX_LINES
    assert all(len(line) <= 120 for line in lines)
    # Kept days are the 10 highest-relevance ones (input order), rendered
    # chronologically; ordinals stay honest against the FULL distinct set.
    day_lines = lines[2:]
    assert len(day_lines) == DERIVED_TIMELINE_MAX_LINES - 2
    assert day_lines == sorted(day_lines)
    assert day_lines[-1].endswith("day 10 of 30")
    # The span line still covers the whole set.
    assert lines[1] == "[derived] dated items span 87 days (2 months 28 days); 2023-01-01 -> 2023-03-29"


def test_derived_timeline_lines_deterministic() -> None:
    events = [_utc(2023, 5, 30, 14), _utc(2023, 5, 20, 7)]
    first = derived_timeline_lines(events, reference_time=_utc(2023, 6, 15))
    second = derived_timeline_lines(list(events), reference_time=_utc(2023, 6, 15))
    assert first == second


# -- compile_context_pack marked block ---------------------------------------------


class _PrecomputeStubStore:
    """Duck-typed minimum surface for compile_context_pack.

    FTS falls back to ``search_memories`` (no ``search_memories_fts``),
    vector/graph/temporal/chunk stages degrade via getattr, and
    ``get_source`` calls are counted so the minimal-depth test can assert
    the provenance-date fallback stays off.
    """

    def __init__(
        self,
        memories: list[dict[str, object]],
        sources: list[dict[str, object]],
        source_rows_by_id: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.memories = memories
        self.sources = sources
        self.source_rows_by_id = source_rows_by_id or {str(row.get("id")): row for row in sources}
        self.get_source_calls = 0
        self.events: list[dict[str, object]] = []

    def search_memories(self, *, query, domains=None, sensitivity_allowed=None, limit=8, **_filters):
        del query, domains, sensitivity_allowed
        return self.memories[:limit]

    def search_sources(self, *, query, domains=None, sensitivity_allowed=None, limit=8, **_filters):
        del query, domains, sensitivity_allowed
        return self.sources[:limit]

    def list_open_loops(self, *, status="open", domains=None, sensitivity_allowed=None, limit=8):
        del status, domains, sensitivity_allowed, limit
        return []

    def list_provenance_links(self, *, target_type, target_id):
        del target_type, target_id
        return []

    def get_source(self, source_id: str):
        self.get_source_calls += 1
        return self.source_rows_by_id.get(str(source_id))

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


def _stub_store() -> _PrecomputeStubStore:
    memories = [
        {
            "id": "memory-valid-from",
            "memory_type": "semantic",
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "canonical_text": "The user adopted a golden retriever puppy.",
            "valid_from": "2023-05-20T07:47:00+00:00",
        },
        {
            "id": "memory-metadata-date",
            "memory_type": "semantic",
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "canonical_text": "The user visited the dog park downtown.",
            "metadata_json": {"session_date": "2023/05/30 (Tue) 02:01"},
        },
        {
            "id": "memory-provenance-date",
            "memory_type": "semantic",
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "canonical_text": "The user bought a leash for the puppy.",
            "metadata_json": {"source_id": "source-dated"},
        },
        {
            "id": "memory-undated",
            "memory_type": "semantic",
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "canonical_text": "The user likes evening walks with the puppy.",
        },
    ]
    sources = [
        {
            "id": "source-dated",
            "title": "Chat session about the puppy",
            "source_type": "chat_session",
            "domain": "unknown",
            "sensitivity": "internal",
            "metadata_json": {"session_date": "2023/06/02 (Fri) 18:30"},
        }
    ]
    return _PrecomputeStubStore(memories, sources)


_QUERY = "golden retriever puppy adoption"  # no date phrase: no temporal anchor
_REFERENCE = _utc(2023, 6, 15, 10, 0)


def _compile(store: _PrecomputeStubStore, **overrides: object) -> dict[str, object]:
    request = VNextRetrievalRequest(
        query=_QUERY,
        include_sources=True,
        **overrides,  # type: ignore[arg-type]
    )
    return VNextRetrievalService(store).compile_context_pack(request, source_fence=SourceReadFence.unfenced())


def test_pack_items_get_iso_event_time_stamps() -> None:
    pack = _compile(_stub_store(), reference_time=_REFERENCE)
    stamps = {str(item["id"]): item.get("event_time") for item in pack["relevant_memories"]}
    assert stamps["memory-valid-from"] == "2023-05-20T07:47:00+00:00"
    assert stamps["memory-metadata-date"] == "2023-05-30T02:01:00+00:00"
    # Provenance fallback: the memory inherits its source's event date.
    assert stamps["memory-provenance-date"] == "2023-06-02T18:30:00+00:00"
    # No content-honest date signal -> no stamp (never the write clock).
    assert stamps["memory-undated"] is None
    assert pack["sources"][0]["event_time"] == "2023-06-02T18:30:00+00:00"


def test_pack_derived_values_block_and_trace_stage() -> None:
    pack = _compile(_stub_store(), reference_time=_REFERENCE)
    derived = pack["derived_values"]
    assert derived["reference_time"] == "2023-06-15T10:00:00+00:00"
    assert derived["lines"] == [
        "[derived] reference date: 2023-06-15 (Thu)",
        "[derived] dated items span 13 days (1 week 6 days); 2023-05-20 -> 2023-06-02",
        "[derived] 2023-05-20 (Sat): 26 days (3 weeks 5 days) earlier; 2023-05-20 -> 2023-06-15; day 1 of 3",
        "[derived] 2023-05-30 (Tue): 16 days (2 weeks 2 days) earlier; 2023-05-30 -> 2023-06-15; day 2 of 3",
        "[derived] 2023-06-02 (Fri): 13 days (1 week 6 days) earlier; 2023-06-02 -> 2023-06-15; day 3 of 3",
    ]
    # 3 dated memories + 1 dated source anchored; the source shares the
    # provenance memory's day, so the block has 3 distinct days.
    assert pack["trace"]["stages"]["temporal_precompute"] == {
        "anchored_items": 4,
        "derived_lines": 5,
    }


def test_pack_derived_block_dormant_without_reference_time() -> None:
    pack = _compile(_stub_store())
    assert "derived_values" not in pack
    assert "temporal_precompute" not in pack["trace"]["stages"]
    # The stamps themselves are unconditional (machine-readable time is
    # presentation, not arithmetic) and identical to the gated pack's.
    gated = _compile(_stub_store(), reference_time=_REFERENCE)
    assert pack["relevant_memories"] == gated["relevant_memories"]
    assert pack["sources"] == gated["sources"]


def test_pack_trace_stage_honest_with_reference_but_no_dated_items() -> None:
    store = _PrecomputeStubStore(
        [
            {
                "id": "memory-undated",
                "memory_type": "semantic",
                "status": "active",
                "domain": "unknown",
                "sensitivity": "internal",
                "canonical_text": "The user likes evening walks.",
            }
        ],
        [],
    )
    pack = _compile(store, reference_time=_REFERENCE)
    assert "derived_values" not in pack
    assert pack["trace"]["stages"]["temporal_precompute"] == {
        "anchored_items": 0,
        "derived_lines": 0,
    }


def test_pack_minimal_depth_never_calls_get_source() -> None:
    store = _stub_store()
    pack = _compile(store, reference_time=_REFERENCE, context_depth=CONTEXT_DEPTH_MINIMAL)
    # Minimal keeps its cheapest-call promise: row-local stamps only.
    assert store.get_source_calls == 0
    stamps = {str(item["id"]): item.get("event_time") for item in pack["relevant_memories"]}
    assert stamps["memory-valid-from"] == "2023-05-20T07:47:00+00:00"
    assert stamps.get("memory-provenance-date") is None  # provenance fallback off
    # Derived block still fires from the row-local dates.
    assert pack["trace"]["stages"]["temporal_precompute"]["anchored_items"] >= 1


def test_pack_derived_block_deterministic_across_compiles() -> None:
    first = _compile(_stub_store(), reference_time=_REFERENCE)
    second = _compile(_stub_store(), reference_time=_REFERENCE)
    assert first["derived_values"] == second["derived_values"]
    assert first["relevant_memories"] == second["relevant_memories"]
    assert first["sources"] == second["sources"]


def test_pack_derived_block_bounded_with_many_dated_items() -> None:
    memories = [
        {
            "id": f"memory-{index:02d}",
            "memory_type": "semantic",
            "status": "active",
            "domain": "unknown",
            "sensitivity": "internal",
            "canonical_text": f"Dated fact number {index} about the puppy.",
            "valid_from": (_utc(2023, 1, 1) + timedelta(days=4 * index)).isoformat(),
        }
        for index in range(40)
    ]
    store = _PrecomputeStubStore(memories, [])
    pack = _compile(store, reference_time=_REFERENCE, max_items=32)
    lines = pack["derived_values"]["lines"]
    assert len(lines) <= DERIVED_TIMELINE_MAX_LINES
    assert pack["trace"]["stages"]["temporal_precompute"]["derived_lines"] == len(lines)
