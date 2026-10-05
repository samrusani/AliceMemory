"""A locked key's report inputs use the exact project test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alicebot_api.vnext_brain import _matches_report_scope
from alicebot_api.vnext_derived_labels import input_admitted, locked_projects, stamp_derived_from

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def test_input_admitted_requires_every_project() -> None:
    alpha = {"metadata_json": {"project_scope": [ALPHA]}}
    shared = {"metadata_json": {"project_scope": [ALPHA, BETA]}}
    global_row = {"metadata_json": {}}
    assert input_admitted("memory", alpha, (ALPHA,)) is True
    assert input_admitted("memory", shared, (ALPHA,)) is False
    assert input_admitted("memory", global_row, (ALPHA,)) is False
    assert input_admitted("memory", alpha, (ALPHA, BETA)) is True


def test_locked_projects_uses_the_binding_when_the_request_is_empty() -> None:
    identity = {"project_scope_locked": True, "project_scope": [ALPHA]}
    assert locked_projects(identity, ()) == (ALPHA,)
    assert locked_projects(identity, (ALPHA,)) == (ALPHA,)
    assert locked_projects({"project_scope_locked": False}, (ALPHA,)) is None
    assert locked_projects(None, (ALPHA,)) is None


def test_a_locked_brief_scope_rejects_a_shared_row() -> None:
    start = datetime(2026, 10, 5, tzinfo=UTC)
    end = start + timedelta(days=1)
    shared = {
        "id": "m",
        "metadata_json": {"project_scope": [ALPHA, BETA]},
        "created_at": start.isoformat(),
    }
    assert _matches_report_scope(
        shared, kind="memory", projects=(ALPHA,), window_start=start, window_end=end
    ) is True
    assert _matches_report_scope(
        shared, kind="memory", projects=(ALPHA,), window_start=start, window_end=end, all_of=(ALPHA,)
    ) is False


def test_stamp_derived_from_counts_match_the_lists() -> None:
    payload: dict[str, object] = {"metadata_json": {"workflow": "daily_brief"}}
    stamp_derived_from(
        payload,
        {
            "sources": [{"id": "s"}],
            "memories": [{"id": "m"}],
            "open_loops": [],
            "artifacts": [],
            "beliefs": [],
        },
    )
    record = payload["metadata_json"]["derived_from"]
    assert record["sources"] == ["s"]
    assert record["counts"]["sources"] == 1
    assert record["counts"]["memories"] == 1
