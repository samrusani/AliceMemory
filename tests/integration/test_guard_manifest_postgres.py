"""Independent guards for bound project updates and saved reference fragments."""

from __future__ import annotations

from dataclasses import replace
import json
from uuid import uuid4

import pytest

from alicebot_api import vnext_hidden_ids, vnext_source_fence
from alicebot_api.vnext_projects import VNextProjectService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.test_project_update_candidate_admission_postgres import _key, _mcp, _project, _stamp_updated_at
from tests.support.legacy_source_refs import link_strings, plant_legacy_source_refs


def _bound_projects(harness, profile):
    mine = _project(harness, name="Bound Kiln")
    newer = _project(harness, name="Other Kiln")
    _stamp_updated_at(harness, mine, "2020-01-01T00:00:00Z")
    _stamp_updated_at(harness, newer, "2021-01-01T00:00:00Z")
    return mine, _key(harness, profile, project=mine)


@pytest.mark.parametrize("profile", ["project_scoped_agent", "admin_agent"])
def test_the_project_tool_supplies_the_key_binding_to_the_service(label_harness, monkeypatch, profile):
    """Changing ``if project_id is None and len(decision.effective_project_scope) == 1:`` to ``if False:`` fails."""
    mine, key = _bound_projects(label_harness, profile)
    original = VNextProjectService.generate_project_update_candidate
    service_projects = []

    def observe(service, request, *, identity=None):
        service_projects.append(request.project_id)
        return original(service, request, identity=identity)

    monkeypatch.setattr(VNextProjectService, "generate_project_update_candidate", observe)
    answer = _mcp(label_harness, key, None)

    assert answer.ok, answer
    assert service_projects == [str(mine["id"])], service_projects
    assert answer.detail["metadata_json"]["project_id"] == str(mine["id"])


@pytest.mark.parametrize("profile", ["project_scoped_agent", "admin_agent"])
def test_the_project_service_filters_the_fallback_by_the_key_binding(label_harness, monkeypatch, profile):
    """Changing ``return [project for project in admitted if project_readable(self.store, project, caller)]``
    to ``return admitted`` fails this test.
    """
    mine, key = _bound_projects(label_harness, profile)
    generate = VNextProjectService.generate_project_update_candidate
    resolve = VNextProjectService._resolve_project
    selected = []

    def without_explicit_project(service, request, *, identity=None):
        # Exercise the service guard with the tool's earlier selection omitted.
        return generate(service, replace(request, project_id=None), identity=identity)

    def observe_selection(service, request, identity):
        project = resolve(service, request, identity)
        selected.append(str(project["id"]))
        return project

    monkeypatch.setattr(VNextProjectService, "generate_project_update_candidate", without_explicit_project)
    monkeypatch.setattr(VNextProjectService, "_resolve_project", observe_selection)
    answer = _mcp(label_harness, key, None)

    assert selected == [str(mine["id"])], selected
    assert answer.ok and answer.detail["metadata_json"]["project_id"] == str(mine["id"]), answer


def _fragment_audit(harness):
    marker = f"FRAGMENT{uuid4().hex}"
    with harness.store() as store:
        cited = store.create_memory(
            {"memory_key": f"fragment-{uuid4().hex}", "canonical_text": "Public note", "title": "Public note",
             "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {}}
        )
    admin = harness.key("admin_agent")
    trusted = harness.key("trusted_local_agent")
    refs = [f"memory:{cited['id']}#{marker}"]
    sent = link_strings(refs)
    status, body, _ = harness.request(
        "POST", "/v0/vnext/memories/commit", key=admin,
        payload={"title": "Follow up", "canonical_text": "Follow up", "memory_type": "fact", "domain": "project",
                 "sensitivity": "public", "confidence": 0.99, "source_type": "agent", "source_refs": sent},
    )
    assert status in {200, 201}, body
    memory_id = str(body["memory"]["id"])
    with harness.store() as store:
        plant_legacy_source_refs(store, memory_id, refs, sent)
    status, body, _ = harness.request("POST", "/v0/vnext/memories/redact", key=admin,
                                      payload={"memory_id": str(cited["id"]), "reason": "guard test"})
    assert status == 200, body
    path = f"/v0/vnext/memories/{memory_id}/audit"
    status, body, _ = harness.request("GET", path, key=admin)
    assert status == 200 and marker in json.dumps(body), body
    return marker, trusted, path


def _observed_audit(harness, monkeypatch, trusted, path):
    bodies = []
    original = vnext_hidden_ids.withhold_json_body

    def observe(request, body, **kwargs):
        if request.url.path == path:
            bodies.append(body)
        return original(request, body, **kwargs)

    monkeypatch.setattr(vnext_hidden_ids, "withhold_json_body", observe)
    status, body, _ = harness.request("GET", path, key=trusted)
    assert status == 200 and len(bodies) == 1, (status, body, len(bodies))
    return bodies[0], json.dumps(body).encode()


def test_the_audit_removes_a_refused_memory_fragment_before_the_response_filter(label_harness, monkeypatch):
    """Changing ``pieces[index] = head`` to ``pieces[index] = word`` fails this test."""
    marker, trusted, path = _fragment_audit(label_harness)
    before_filter, response = _observed_audit(label_harness, monkeypatch, trusted, path)
    assert marker.encode() not in before_filter
    assert marker.encode() not in response


def test_the_http_filter_withholds_a_fragment_that_passes_the_reference_guard(label_harness, monkeypatch):
    """Changing ``filtered = withhold_with(open_store, identity, payload, echo=echo)`` to ``filtered = payload`` fails."""
    marker, trusted, path = _fragment_audit(label_harness)
    original = vnext_source_fence._canonical_references

    def keep_fragment(text):
        result = original(text)
        return text if "#" in text and result is not None else result

    with monkeypatch.context() as context:
        # Exercise the later filter after the reference guard lets the fragment through.
        context.setattr(vnext_source_fence, "_canonical_references", keep_fragment)
        before_filter, response = _observed_audit(label_harness, context, trusted, path)
    assert marker.encode() in before_filter
    assert marker.encode() not in response
