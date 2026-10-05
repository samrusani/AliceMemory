"""Every report producer stores a sensitivity at least as strict as each input row it prints.

The artifact read decides by the stored label alone. For each producer this test raises one input row at a time to
``confidential``, runs the producer, and looks at what it printed (the text and the metadata of the report). If the
id or the text of the raised row is in the output, the stored sensitivity has to be ``confidential`` or stricter.
The printed output is the judge, not the producer's own list of inputs, so a row that a producer prints but leaves
out of its label fails here by name. Each producer must also print at least one of its inputs, so a sweep that
prints nothing cannot pass.

The consolidation report and the open-loop review have their own tests (``test_consolidation_report_label.py``,
``test_consolidation_report_names_sources.py`` and ``test_open_loop_review_label.py``); the producers here are the ones
the sweep found already correct. The daily brief and the weekly synthesis read four kinds of input (sources, memories,
open loops and stored artifacts), and the sweep raises each of them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

import pytest

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_connections import ConnectionFinderRequest, VNextConnectionService
from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService

RANK = {
    "public": 1,
    "internal": 2,
    "unknown": 2,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}
EVERYTHING = ("public", "internal", "private", "confidential", "unknown")
Builder = Callable[[], tuple[object, Callable[[object], list[dict]], Callable[[object], dict]]]


# A report the store already holds, which the daily brief and the weekly synthesis read as context and print by id
# and title. It is an input row like the others, so the sweep raises it too. The reports a run writes are not inputs
# and are told apart by their type.
SEEDED_ARTIFACT_TYPE = "research_note"


def _brain(workflow: str) -> Builder:
    def build():
        from tests.unit.test_vnext_brain import _seed_store

        store = _seed_store()
        store.artifacts["seeded-note-1"] = {
            "id": "seeded-note-1",
            "artifact_type": SEEDED_ARTIFACT_TYPE,
            "title": "Quarterly planning notes",
            "status": "reviewed",
            "domain": "project",
            "sensitivity": "private",
            "created_at": "2026-05-10T07:30:00Z",
            "metadata_json": {},
        }

        def rows(current) -> list[dict]:
            seeded = [row for row in current.artifacts.values() if row.get("artifact_type") == SEEDED_ARTIFACT_TYPE]
            return [*current.sources, *current.memories, *current.open_loops, *seeded]

        def run(current) -> dict:
            service = VNextBrainService(current)
            request = BrainArtifactRequest(generated_for="2026-05-10", sensitivity_allowed=EVERYTHING)
            return getattr(service, f"generate_{workflow}")(request)

        return store, rows, run

    return build


def _connections() -> tuple[object, Callable, Callable]:
    from tests.unit.test_vnext_connections import _seed_store

    store = _seed_store()
    return (
        store,
        lambda current: [*current.sources, *current.memories],
        lambda current: VNextConnectionService(current).generate_connection_report(
            ConnectionFinderRequest(sensitivity_allowed=EVERYTHING)
        ),
    )


def _contradictions() -> tuple[object, Callable, Callable]:
    from tests.unit.test_vnext_contradictions import _seed_store

    store = _seed_store()
    return (
        store,
        lambda current: [*current.sources, *current.memories, *current.beliefs.values()],
        lambda current: VNextContradictionService(current).generate_contradiction_report(
            ContradictionFinderRequest(sensitivity_allowed=EVERYTHING)
        ),
    )


def _project_update() -> tuple[object, Callable, Callable]:
    from tests.unit.test_vnext_projects import _seed_store

    store = _seed_store()
    return (
        store,
        lambda current: [*current.projects.values(), *current.sources, *current.memories.values()],
        lambda current: VNextProjectService(current).generate_project_update_candidate(
            ProjectAutomationRequest(project_id="project-1", sensitivity_allowed=EVERYTHING)
        ),
    )


def _staleness() -> tuple[object, Callable, Callable]:
    from tests.unit.test_vnext_scheduler import _staleness_store

    store = _staleness_store()
    return (
        store,
        lambda current: list(current.memories),
        lambda current: VNextSchedulerService(current).run_now(
            SchedulerRunRequest(
                workflow_type="staleness_sweep",
                sensitivity_allowed=EVERYTHING,
                generated_for="2026-07-04",
                options={"reference_time": "2026-07-04T03:30:00Z"},
            )
        )["artifact"],
    )


PRODUCERS: dict[str, Callable] = {
    "daily_brief": _brain("daily_brief"),
    "weekly_synthesis": _brain("weekly_synthesis"),
    "connection_report": _connections,
    "contradiction_report": _contradictions,
    "project_update": _project_update,
    "staleness_sweep": _staleness,
}


def _markers(row: dict) -> set[str]:
    """What identifies the row in a printed report: its id, and any text of it long enough to be its own."""

    found = {str(row["id"])}
    for key in ("title", "name", "canonical_text", "claim", "summary"):
        value = row.get(key)
        if isinstance(value, str) and len(value.strip()) >= 8:
            found.add(value.strip())
    metadata = row.get("metadata_json")
    raw_text = metadata.get("raw_text") if isinstance(metadata, dict) else None
    if isinstance(raw_text, str):
        found.update(line.strip() for line in raw_text.splitlines() if len(line.strip()) >= 8)
    return found


def _printed(artifact: dict) -> str:
    return (artifact["content_markdown"] + "\n" + json.dumps(artifact["metadata_json"], default=str)).casefold()


def _count(rows: Iterable[dict]) -> int:
    return len(list(rows))


@pytest.mark.parametrize("producer", sorted(PRODUCERS))
def test_a_row_the_report_prints_is_covered_by_its_label(producer: str) -> None:
    build = PRODUCERS[producer]
    store, rows, _run = build()
    total = _count(rows(store))
    assert total >= 2
    printed_any = False
    for index in range(total):
        store, rows, run = build()
        before = {str(item["id"]) for item in rows(store)}
        row = rows(store)[index]
        row["sensitivity"] = "confidential"
        artifact = run(store)
        printed = _printed(artifact)
        shown = sorted(marker for marker in _markers(row) if marker.casefold() in printed)
        printed_any = printed_any or bool(shown)
        if shown:
            assert RANK[str(artifact["sensitivity"])] >= RANK["confidential"], (
                producer,
                f"row {index} ({row['id']}) is printed as {shown[:2]} under sensitivity {artifact['sensitivity']}",
            )
        # The rows a producer writes next to its report (candidate memories, candidate loops) are labelled by the
        # same rule: one that carries the id or the text of the raised row is at least as strict as it.
        for created in rows(store):
            if str(created["id"]) in before:
                continue
            carried = sorted(
                marker for marker in _markers(row) if marker.casefold() in json.dumps(created, default=str).casefold()
            )
            if carried:
                assert RANK[str(created.get("sensitivity", "unknown"))] >= RANK["confidential"], (
                    producer,
                    f"row {index} ({row['id']}) is carried as {carried[:2]} by new row {created['id']} "
                    f"under sensitivity {created.get('sensitivity')}",
                )
    assert printed_any, f"{producer} printed none of its inputs, so the sweep proved nothing"


@pytest.mark.parametrize("workflow", ("daily_brief", "weekly_synthesis"))
def test_the_sweep_reaches_the_stored_artifact_the_brain_reports_read(workflow: str) -> None:
    """The artifact input is only proved if the report prints it. If a change stops the brain reports printing the
    stored artifact, this fails by name instead of the sweep passing over an input it never reads."""

    store, rows, run = _brain(workflow)()
    seeded = [row for row in rows(store) if row.get("artifact_type") == SEEDED_ARTIFACT_TYPE]
    assert len(seeded) == 1
    printed = _printed(run(store))
    assert str(seeded[0]["id"]).casefold() in printed
    assert str(seeded[0]["title"]).casefold() in printed
