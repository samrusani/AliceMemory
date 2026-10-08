"""The doctor routes show content diagnostics only to a caller with no limits.

Two doctor checks read the vault's content: ``flagged_sources`` lists the ids of sources that carry credential
material, whatever their sensitivity, and ``derived_labels`` counts the rows stored below their inputs and the rows
whose inputs cannot be checked, whatever the label of those rows. A key with a ceiling (``trusted_local_agent``)
passes the operator gate, so before this change it received both for rows it may not read.

These tests run the mounted application on role-separated Postgres. The vault holds a confidential source with
credential material and a public memory stored below a confidential input. The owner and an unbound admin key must
receive both checks exactly as the doctor service computes them. A trusted key must receive both as skipped, with no
id, count or message taken from the vault, and every other check as before. The operator gate keeps refusing the
profiles and project-bound keys it refused.
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
import pytest

from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

CONTENT_CHECKS = ("flagged_sources", "derived_labels")
ROUTES = (
    pytest.param("GET", "/v0/vnext/doctor", None, id="get"),
    pytest.param("POST", "/v0/vnext/doctor/run", {"fix_safe": False, "ci": True}, id="post"),
)
REPAIR_COMMAND = "alicebot vnext labels repair"
SKIPPED_MESSAGE = "Content diagnostics are available to the owner and an unbound admin key."


def _token() -> str:
    # Built at run time so no credential-shaped literal sits in the repository.
    return "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _seed_vault(h):
    """A flagged confidential source and a public memory stored below a confidential input."""

    flagged = h.source(sensitivity="confidential", text=f"deploy token {_token()}")
    parent = h.source(sensitivity="confidential")
    with h.store() as store, without_insert_floor():
        child = store.create_memory(
            {
                "memory_key": str(uuid4()),
                "canonical_text": "Synthetic memory stored below its input",
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"project_scope": [], "source_id": str(parent["id"])},
            }
        )
        assert child["sensitivity"] == "public"
    with h.store() as store:
        assert label_gap_counts(store) == (1, 0)
    return {"flagged": str(flagged["id"]), "parent": str(parent["id"]), "child": str(child["id"])}


def _call(h, method, path, body, *, key=None):
    return h.request(method, path, payload=body, key=key)


def _checks(body):
    return {check["name"]: check for check in body["checks"]}


def _service_payload(h, **options):
    with h.store() as store:
        return json.loads(json.dumps(jsonable_encoder(VNextDoctorService(store).run(**options))))


def _without_content(body):
    kept = dict(body)
    kept["checks"] = [check for check in body["checks"] if check["name"] not in CONTENT_CHECKS]
    for field in ("status", "warning_count", "blocking_failure_count", "recommended_fixes"):
        kept.pop(field)
    return kept


def _assert_full_content_checks(body, ids):
    checks = _checks(body)
    flagged = checks["flagged_sources"]
    assert flagged["status"] == "fail" and flagged["details"]["source_ids"] == [ids["flagged"]]
    assert flagged["details"]["count"] == 1
    derived = checks["derived_labels"]
    assert derived["status"] == "fail"
    assert derived["message"] == "derived labels: 1 below their inputs, 0 unverified"


def _assert_nothing_from_content(raw, ids):
    for identifier in ids.values():
        assert identifier not in raw
    for fragment in ("source_ids", "stored sources carry credential material", "below their inputs", "unverified"):
        assert fragment not in raw, fragment


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize("profile", ("owner", "admin_agent"))
def test_owner_and_unbound_admin_receive_the_full_doctor(label_harness, profile, method, path, body):
    h = label_harness
    ids = _seed_vault(h)
    # The owner has no key, so it must call before the first key exists.
    key = None if profile == "owner" else h.key("admin_agent")
    status, payload, _headers = _call(h, method, path, body, key=key)
    assert status == 200, payload
    _assert_full_content_checks(payload, ids)
    assert payload == _service_payload(h, ci=True)
    # Both content checks are warnings on top of whatever the rest of the doctor reports.
    skipped = _service_payload(h, ci=True, include_content_diagnostics=False)
    assert payload["warning_count"] == skipped["warning_count"] + 2
    assert REPAIR_COMMAND in payload["recommended_fixes"] and REPAIR_COMMAND not in skipped["recommended_fixes"]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_unbound_trusted_key_receives_content_checks_as_skipped(label_harness, method, path, body):
    h = label_harness
    ids = _seed_vault(h)
    status, payload, _headers = _call(h, method, path, body, key=h.key("trusted_local_agent"))
    assert status == 200, payload
    raw = json.dumps(payload)
    _assert_nothing_from_content(raw, ids)
    checks = _checks(payload)
    for name in CONTENT_CHECKS:
        assert checks[name] == {
            "name": name,
            "status": "skipped",
            "severity": "info",
            "message": SKIPPED_MESSAGE,
            "recommended_fix": None,
            "details": {"scope": "filtered_workspace", "evaluated": False},
        }
    # Every other check and field is what the service returns when it is asked not to read content.
    assert payload == _service_payload(h, ci=True, include_content_diagnostics=False)
    assert REPAIR_COMMAND not in payload["recommended_fixes"]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_only_the_content_checks_differ_between_the_owner_and_a_trusted_key(label_harness, method, path, body):
    h = label_harness
    _seed_vault(h)
    owner_status, owner, _headers = _call(h, method, path, body)
    assert owner_status == 200, owner
    trusted_status, trusted, _headers = _call(h, method, path, body, key=h.key("trusted_local_agent"))
    assert trusted_status == 200, trusted
    assert [check["name"] for check in owner["checks"]] == [check["name"] for check in trusted["checks"]]
    assert _without_content(owner) == _without_content(trusted)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize(
    ("profile", "project"),
    (
        ("read_only_agent", None),
        ("memory_proposal_agent", None),
        ("project_scoped_agent", "P1"),
        ("trusted_local_agent", "P1"),
        ("admin_agent", "P1"),
    ),
)
def test_the_operator_gate_still_refuses_profiles_without_it(label_harness, profile, project, method, path, body):
    h = label_harness
    ids = _seed_vault(h)
    status, payload, _headers = _call(h, method, path, body, key=h.key(profile, project=project))
    assert status == 403, payload
    _assert_nothing_from_content(json.dumps(payload), ids)
    assert "checks" not in payload


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_a_wrong_or_revoked_key_gets_the_workspace_answer(label_harness, method, path, body):
    h = label_harness
    ids = _seed_vault(h)
    with h.store() as store:
        record, revoked = create_agent_key(
            store, user_id=h.user_id, agent_id=str(uuid4()), permission_profile="trusted_local_agent"
        )
        store.revoke_agent_api_key(key_id=str(record["id"]))
    h.key("admin_agent")
    for key in ("alice_sk_" + "0" * 40, revoked, None):
        expected = h.request("GET", "/v0/vnext/workspace", key=key)
        actual = _call(h, method, path, body, key=key)
        assert actual[0] == expected[0] and actual[0] in (401, 403)
        assert actual[1] == expected[1]
        _assert_nothing_from_content(json.dumps(actual[1]), ids)


def test_fix_safe_still_prepares_connector_defaults_for_a_trusted_key(label_harness):
    h = label_harness
    ids = _seed_vault(h)
    key = h.key("trusted_local_agent")
    with h.store() as store:
        assert store.list_connector_settings() == []
    status, payload, _headers = _call(h, "POST", "/v0/vnext/doctor/run", {"fix_safe": True, "ci": True}, key=key)
    assert status == 200, payload
    assert payload["fix_safe_applied"] is True
    _assert_nothing_from_content(json.dumps(payload), ids)
    with h.store() as store:
        assert {row["connector_name"] for row in store.list_connector_settings()} >= {"telegram"}
    assert payload == _service_payload(h, fix_safe=True, ci=True, include_content_diagnostics=False)


@pytest.mark.parametrize("authorization", ("Bearer alice_sk_" + "0" * 40, None))
@pytest.mark.parametrize("kind", ("get", "post"))
def test_the_handlers_answer_a_refused_key_like_the_workspace_handler(label_harness, kind, authorization):
    """The handlers authenticate on their own, so a key refused after the gate answers as the workspace does."""

    from alicebot_api.routers import vnext_memories as router
    from alicebot_api.routers import workspaces

    h = label_harness
    ids = _seed_vault(h)
    h.key("trusted_local_agent")
    expected = workspaces.get_vnext_workspace(h.user_id, authorization=authorization)
    if kind == "get":
        actual = router.get_vnext_doctor(h.user_id, ci=True, authorization=authorization)
    else:
        request = router.VNextDoctorRunRequest(user_id=h.user_id, fix_safe=False, ci=True)
        actual = router.run_vnext_doctor(request, authorization=authorization)
    assert actual.status_code == expected.status_code == 401
    assert actual.body == expected.body
    _assert_nothing_from_content(actual.body.decode(), ids)
