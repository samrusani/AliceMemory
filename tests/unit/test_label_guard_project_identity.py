"""Original project identities survive filtering without widening derived scope."""
from __future__ import annotations

from uuid import UUID

import pytest

from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def _guard(store: object, projects: tuple[str, ...], *, all_of: tuple[str, ...] | None = None) -> LabelGuard:
    return LabelGuard.for_filters(store, None, ("public",), projects, all_of=all_of)


@pytest.mark.parametrize("field", ("id", "slug", "name"))
def test_original_project_is_admitted_by_its_own_identity(field: str) -> None:
    row = {"id": "project-id", "slug": "other", "name": "Other", "sensitivity": "public", field: ALPHA.upper()}
    assert _guard(object(), (ALPHA,)).admit_rows("project", [row]) == [row]


def test_original_postgres_uuid_project_id_is_compared_as_text() -> None:
    project_id = UUID("12345678-1234-1234-1234-123456abcdef")
    row = {"id": project_id, "slug": "other", "sensitivity": "public"}
    assert _guard(object(), (str(project_id).upper(),)).admit_rows("project", [row]) == [row]


@pytest.mark.parametrize("scope", ([], [BETA]))
@pytest.mark.parametrize("at_root", (False, True))
def test_canonical_original_project_scope_does_not_widen_to_its_slug(scope: list[str], at_root: bool) -> None:
    row = {"id": "project-id", "slug": ALPHA, "sensitivity": "public"}
    row["project_scope" if at_root else "metadata_json"] = scope if at_root else {"project_scope": scope}
    assert _guard(object(), (ALPHA,)).admit_rows("project", [row]) == []


def test_original_project_identity_does_not_bypass_its_floor_or_all_input_binding() -> None:
    row = {"id": "project-id", "slug": "original-project", "sensitivity": "public", "metadata_json": {"project_floor": [BETA]}}
    assert _guard(object(), (GLOBAL_PROJECT_MARKER,)).admit_rows("project", [row]) == []
    assert _guard(object(), (GLOBAL_PROJECT_MARKER, BETA)).admit_rows("project", [row]) == [row]
    assert _guard(object(), (), all_of=(ALPHA, BETA)).admit_rows("project", [row]) == []


def test_derived_project_slug_does_not_replace_its_effective_input_scope() -> None:
    source = {"id": "source-id", "sensitivity": "public", "metadata_json": {"project_scope": [BETA]}}

    class Store:
        def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
            return [source] if kind == "source" and source["id"] in ids else []

    project = {"id": "project-id", "slug": ALPHA, "sensitivity": "public", "metadata_json": {
        "derived_from": {"v": 1, "sources": [source["id"]], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [],
                         "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}},
    }}
    assert _guard(Store(), (ALPHA,)).admit_rows("project", [project]) == []
    assert _guard(Store(), (BETA,)).admit_rows("project", [project]) == []
    assert _guard(Store(), (), all_of=(ALPHA,)).admit_rows("project", [project]) == []
    assert _guard(Store(), (), all_of=(BETA,)).admit_rows("project", [project]) == []
    assert _guard(Store(), ()).admit_rows("project", [project]) == [project]
