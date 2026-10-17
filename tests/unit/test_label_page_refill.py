"""A key with limits gets a full page after rows it may not read are dropped.

The vector stage, the temporal-anchor stage, the context tree, the workspace lists and the project list used to
take a page and then drop. A hidden row used a place. They now read at most ``READABLE_SCAN_CAP`` rows of each list
and return the first rows the caller may read. The owner and an unbound admin key are still read once, at the limit
they asked for.

Mutation: in ``page_after_label_drop``, read ``want`` rows when ``refill`` is true. ``test_a_limited_page_matches_the_vault_without_the_hidden_rows``
fails, because the four nearest rows are hidden and the page comes back empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from alicebot_api.project_view import page_after_label_drop
from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_retrieval import VECTOR_STAGE_ENABLED, VNextRetrievalService
from alicebot_api.vnext_temporal_query import TemporalAnchor


def _row(name: str, *, hidden: bool) -> dict:
    return {
        "id": name,
        "domain": "project",
        "sensitivity": "confidential" if hidden else "public",
        "status": "active",
        "canonical_text": name,
        "vector_distance": 0.01 if hidden else 0.2,
    }


def _keep(rows):
    return [row for row in rows if row["sensitivity"] == "public"]


class _Rank:
    def __init__(self, rows):
        self.rows = list(rows)
        self.limits: list[int] = []

    def fetch(self, limit: int):
        self.limits.append(limit)
        return self.rows[:limit]


def test_a_limited_page_matches_the_vault_without_the_hidden_rows() -> None:
    hidden = [_row(f"hidden-{index}", hidden=True) for index in range(10)]
    visible = [_row(f"visible-{index}", hidden=False) for index in range(8)]
    mixed = _Rank([*hidden, *visible])
    clean = _Rank(visible)
    mixed_page = page_after_label_drop(want=4, fetch=mixed.fetch, keep=_keep, refill=True)
    clean_page = page_after_label_drop(want=4, fetch=clean.fetch, keep=_keep, refill=True)
    assert [row["id"] for row in mixed_page] == [row["id"] for row in clean_page] == [f"visible-{index}" for index in range(4)]
    assert mixed.limits == [READABLE_SCAN_CAP]
    assert clean.limits == [READABLE_SCAN_CAP]
    longer = page_after_label_drop(want=6, fetch=_Rank([*hidden, *visible]).fetch, keep=_keep, refill=True)
    assert [row["id"] for row in mixed_page] == [row["id"] for row in longer][:4]


def test_the_owner_is_read_once_at_the_limit_asked_for() -> None:
    hidden = [_row(f"hidden-{index}", hidden=True) for index in range(10)]
    visible = [_row(f"visible-{index}", hidden=False) for index in range(8)]
    rank = _Rank([*hidden, *visible])
    page = page_after_label_drop(want=4, fetch=rank.fetch, keep=_keep, refill=False)
    assert page == []
    assert rank.limits == [4]


def test_ten_thousand_hidden_nearest_rows_are_judged_at_the_cap() -> None:
    hidden = [_row(f"hidden-{index}", hidden=True) for index in range(10_000)]
    visible = [_row("visible", hidden=False)]
    rank = _Rank([*hidden, *visible])
    page = page_after_label_drop(want=4, fetch=rank.fetch, keep=_keep, refill=True)
    assert page == []
    assert rank.limits == [READABLE_SCAN_CAP]
    assert 10_000 not in rank.limits


def test_a_page_at_the_cap_is_not_widened() -> None:
    rank = _Rank([_row(f"hidden-{index}", hidden=True) for index in range(READABLE_SCAN_CAP + 5)])
    page = page_after_label_drop(want=READABLE_SCAN_CAP, fetch=rank.fetch, keep=_keep, refill=True)
    assert page == []
    assert rank.limits == [READABLE_SCAN_CAP]


class _VectorStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.limits: list[int] = []

    def search_memories_vector(self, *, limit, **_kwargs):
        self.limits.append(limit)
        return [dict(row) for row in self.rows[:limit]]

    def search_memories_by_time(self, *, limit, **_kwargs):
        self.limits.append(limit)
        return [dict(row) for row in self.rows[:limit]]


def _service(store):
    return VNextRetrievalService(
        store,
        embedding_provider=SimpleNamespace(provider="synthetic", model="synthetic", base_url="http://127.0.0.1:9"),
    )


def test_the_vector_stage_returns_the_same_rows_with_and_without_hidden_rows() -> None:
    hidden = [_row(f"hidden-{index}", hidden=True) for index in range(6)]
    visible = [_row(f"visible-{index}", hidden=False) for index in range(5)]
    for index, row in enumerate(visible):
        row["vector_distance"] = 0.4 + index / 100
    mixed = _VectorStore([*hidden, *visible])
    clean = _VectorStore(visible)
    sensitivity = ["public", "internal", "private", "unknown"]
    mixed_rows, mixed_stage = _service(mixed)._memory_vector_rows(
        query="unrelated",
        domains=["project"],
        sensitivity_allowed=sensitivity,
        limit=4,
        query_vector=[1.0, 0.0],
        query_embedding_status=VECTOR_STAGE_ENABLED,
        label_refill=True,
    )
    clean_rows, clean_stage = _service(clean)._memory_vector_rows(
        query="unrelated",
        domains=["project"],
        sensitivity_allowed=sensitivity,
        limit=4,
        query_vector=[1.0, 0.0],
        query_embedding_status=VECTOR_STAGE_ENABLED,
        label_refill=True,
    )
    assert mixed_stage == clean_stage == VECTOR_STAGE_ENABLED
    assert [row["id"] for row in mixed_rows] == [row["id"] for row in clean_rows]
    assert len(mixed_rows) == 4
    assert mixed.limits == [READABLE_SCAN_CAP]


def test_the_temporal_stage_refills_for_a_caller_with_limits_only() -> None:
    hidden = [_row(f"hidden-{index}", hidden=True) for index in range(3)]
    visible = [_row(f"visible-{index}", hidden=False) for index in range(3)]
    limited = _VectorStore([*hidden, *visible])
    owner = _VectorStore([*hidden, *visible])
    anchor = TemporalAnchor(datetime.now(UTC) - timedelta(days=1), datetime.now(UTC), "synthetic")
    sensitivity = ["public", "internal", "private", "unknown"]
    limited_rows, _stage = _service(limited)._memory_temporal_rows(
        anchor=anchor, domains=["project"], sensitivity_allowed=sensitivity, limit=2, label_refill=True
    )
    owner_rows, _stage = _service(owner)._memory_temporal_rows(
        anchor=anchor, domains=["project"], sensitivity_allowed=sensitivity, limit=2, label_refill=False
    )
    assert [row["id"] for row in limited_rows] == ["visible-0", "visible-1"]
    assert owner_rows == []
    assert limited.limits == [READABLE_SCAN_CAP]
    assert owner.limits == [2]


class _TreeStore:
    def __init__(self, projects):
        self.projects = list(projects)
        self.limits: list[int] = []

    def list_projects(self, *, limit, **_kwargs):
        self.limits.append(("projects", limit))
        return [dict(row) for row in self.projects[:limit]]

    def search_memories(self, *, limit, **_kwargs):
        self.limits.append(("memories", limit))
        return []

    def search_sources(self, *, limit, **_kwargs):
        self.limits.append(("sources", limit))
        return []

    def list_open_loops(self, *, limit, **_kwargs):
        self.limits.append(("loops", limit))
        return []

    def list_artifacts(self, *, limit, **_kwargs):
        self.limits.append(("artifacts", limit))
        return []

    def list_events(self, *, limit, **_kwargs):
        self.limits.append(("events", limit))
        return []

    def append_event(self, event):
        return event


def test_the_context_tree_fills_its_page_for_a_limited_caller_and_not_for_the_owner() -> None:
    projects = [_row(f"hidden-{index}", hidden=True) for index in range(12)]
    projects.extend(_row(f"visible-{index}", hidden=False) for index in range(4))
    for row in projects:
        row.update(name=row["id"], slug=row["id"], status="active")
    limited = _TreeStore(projects)
    owner = _TreeStore(projects)
    sensitivity = ("public", "internal", "private", "unknown")
    limited_tree = VNextContextTreeService(limited).build_tree(
        ContextTreeRequest(limit=12, sensitivity_allowed=sensitivity, include_events=False, caller_limited=True)
    )
    owner_tree = VNextContextTreeService(owner).build_tree(
        ContextTreeRequest(limit=12, sensitivity_allowed=sensitivity, include_events=False, caller_limited=False)
    )
    limited_ids = [node["ref"] for node in limited_tree["roots"][0]["children"]]
    owner_ids = [node["ref"] for node in owner_tree["roots"][0]["children"]]
    assert limited_ids == [f"project:visible-{index}" for index in range(4)]
    assert owner_ids == []
    assert ("projects", READABLE_SCAN_CAP) in limited.limits
    assert ("projects", 12) in owner.limits
