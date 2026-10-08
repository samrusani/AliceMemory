"""The owner workspace shows the recorded result of the last full labels check.

A page load cannot settle every derived row. The doctor command and `labels check`
keep the live full check and record what they find. The workspace reads the newest
record, shows its time, and says when the label inputs changed after it.
"""
from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alicebot_api.cli import labels
from alicebot_api.cli.capture import _run_vnext_doctor
from alicebot_api.routers.workspaces import _vnext_workspace_payload
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_check_record import (
    LABEL_CHECK_EVENT,
    label_input_fingerprint,
    recorded_label_check,
)
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_label_writes import without_insert_floor

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401

ADMIN = AgentIdentity(agent_id="recorded-admin", permission_profile="admin_agent")
TRUSTED = AgentIdentity(agent_id="recorded-trusted", permission_profile="trusted_local_agent")


def context(h):
    return SimpleNamespace(database_url=h.urls["app"], user_id=h.user_id)


def make_gap(h):
    """A report stored public whose input source is confidential."""
    hidden = h.source(sensitivity="confidential")
    with h.store() as store, without_insert_floor():
        report = store.create_memory({
            "memory_key": str(uuid4()), "canonical_text": "Synthetic report", "status": "active",
            "domain": "project", "sensitivity": "public",
            "metadata_json": with_derived_from({"workflow": "project_auto_update"}, {"sources": [hidden]}),
        })
    return hidden, report


def derived_check(h, identity=ADMIN):
    with h.store() as store:
        payload = _vnext_workspace_payload(store, identity=identity)
    return payload, next(row for row in payload["doctor"]["checks"] if row["name"] == "derived_labels")


def run_check(h):
    try:
        return labels._run_vnext_labels_check(context(h), None)
    except SystemExit as exit_:
        assert exit_.code == 1
        return "gap"


def never_settle(monkeypatch):
    """The workspace must not run the full check, so make the full check unusable."""
    from alicebot_api import vnext_label_repair

    def refuse(*args, **kwargs):
        raise AssertionError("the workspace settled every derived row")

    monkeypatch.setattr(vnext_label_repair, "classify_stored_labels", refuse)
    monkeypatch.setattr(vnext_label_repair, "label_gap_report", refuse)


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "admin"])
def test_workspace_shows_the_recorded_gap_after_a_check_runs(label_harness, monkeypatch, identity):
    h = label_harness
    make_gap(h)
    with h.store() as store:
        assert label_gap_counts(store) == (1, 0)
        live = next(c for c in VNextDoctorService(store).run(ci=True)["checks"] if c["name"] == "derived_labels")
    assert live["status"] == "fail" and live["severity"] == "warning"
    assert live["message"] == "derived labels: 1 below their inputs, 0 unverified"

    with monkeypatch.context() as patch:
        never_settle(patch)
        _payload, before = derived_check(h, identity)
        assert before["status"] == "skipped"
        assert before["details"] == {"scope": "recorded", "recorded": False, "evaluated": False, "unreadable": False}
        assert "no recorded check" in before["message"]

        assert run_check(h) == "gap"
        payload, after = derived_check(h, identity)
    assert after["status"] == "fail" and after["severity"] == "warning"
    assert after["message"].startswith("derived labels: 1 below their inputs, 0 unverified (recorded by labels check at ")
    assert after["recommended_fix"] == "alicebot vnext labels repair"
    details = after["details"]
    assert (details["recorded"], details["evaluated"], details["cause"], details["changed_since"]) == (True, True, "labels_check", False)
    assert (details["below_inputs"], details["unverified"]) == (1, 0)
    assert datetime.fromisoformat(details["checked_at"].replace("Z", "+00:00")).year >= 2026
    assert details["checked_at"] in after["message"]
    assert "alicebot vnext labels repair" in payload["doctor"]["recommended_fixes"]
    assert payload["doctor"]["warning_count"] >= 1


def make_unverified(h):
    """A derived row whose marker is malformed: a repair leaves it alone and a check counts it."""
    with h.store() as store, without_insert_floor():
        store.create_memory({
            "memory_key": str(uuid4()), "canonical_text": "Synthetic malformed report", "status": "active",
            "domain": "project", "sensitivity": "public", "metadata_json": {"candidate_kind": []},
        })


def test_repair_records_what_a_full_check_finds_after_it(label_harness, monkeypatch):
    """Mutation: record zero unverified rows after a repair, whatever is left."""
    h = label_harness
    make_gap(h)
    make_unverified(h)
    with h.store() as store:
        assert label_gap_counts(store) == (1, 1)
    assert labels._run_vnext_labels_repair(context(h), None).startswith("labels repair updated 1")
    with monkeypatch.context() as patch:
        never_settle(patch)
        _payload, after = derived_check(h)
    assert after["status"] == "fail"
    assert after["details"]["cause"] == "labels_repair"
    assert (after["details"]["below_inputs"], after["details"]["unverified"]) == (0, 1)
    assert after["message"].startswith("derived labels: 0 below their inputs, 1 unverified (recorded by labels repair at ")
    with h.store() as store:
        assert label_gap_counts(store) == (0, 1)


def test_repair_and_doctor_command_record_and_changed_data_is_noted(label_harness, monkeypatch):
    h = label_harness
    make_gap(h)
    run_check(h)
    assert labels._run_vnext_labels_repair(context(h), None).startswith("labels repair updated ")
    with monkeypatch.context() as patch:
        never_settle(patch)
        _payload, repaired = derived_check(h)
    assert repaired["status"] == "pass"
    assert repaired["details"]["cause"] == "labels_repair"
    assert (repaired["details"]["below_inputs"], repaired["details"]["unverified"]) == (0, 0)
    assert repaired["details"]["changed_since"] is False
    assert "changed" not in repaired["message"]

    # A new gap after the recorded check: the workspace still shows that check and says so.
    make_gap(h)
    with monkeypatch.context() as patch:
        never_settle(patch)
        _payload, stale = derived_check(h)
    assert stale["status"] == "pass"
    assert stale["details"]["changed_since"] is True
    assert stale["details"]["checked_at"] == repaired["details"]["checked_at"]
    assert "have changed since this check" in stale["message"]
    with h.store() as store:
        assert label_gap_counts(store) == (1, 0)

    # The doctor command is a live full check and records it.
    _run_vnext_doctor(context(h), SimpleNamespace(fix_safe=True, ci=True))
    with monkeypatch.context() as patch:
        never_settle(patch)
        _payload, current = derived_check(h)
    assert current["status"] == "fail"
    assert (current["details"]["cause"], current["details"]["below_inputs"], current["details"]["changed_since"]) == ("doctor", 1, False)


def test_a_failed_write_of_the_record_never_changes_the_check(label_harness, monkeypatch, capsys):
    h = label_harness
    make_gap(h)
    monkeypatch.setattr(labels, "record_label_check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic")))
    assert run_check(h) == "gap"
    captured = capsys.readouterr()
    assert "below_inputs 1" in captured.out
    assert captured.err.strip() == "labels check result was not recorded"
    with h.store() as store:
        assert recorded_label_check(store) is None


def test_trusted_workspace_neither_reads_nor_lists_the_record(label_harness):
    h = label_harness
    make_gap(h)
    run_check(h)
    owner_payload, owner_check = derived_check(h, ADMIN)
    assert owner_check["details"]["recorded"] is True
    assert LABEL_CHECK_EVENT in {row["event_type"] for row in owner_payload["recent_events"]}
    payload, fenced = derived_check(h, TRUSTED)
    assert fenced["status"] == "skipped" and fenced["details"] == {"scope": "filtered_workspace", "evaluated": False}
    assert LABEL_CHECK_EVENT not in json.dumps(payload, default=str)
    assert "below their inputs" not in json.dumps(payload["doctor"], default=str)
    # The event is not counted for a fenced caller either.
    with h.store() as store:
        from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope

        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=("public", "internal", "private", "unknown"))
            events = store.list_events(limit=50)
            assert LABEL_CHECK_EVENT in {row["event_type"] for row in events}
            assert LABEL_CHECK_EVENT not in {row["event_type"] for row in guard.admit_events(events)}


@pytest.mark.parametrize("payload", [
    {"below_inputs": "0", "unverified": 0, "fingerprint": "x", "cause": "labels_check"},
    {"below_inputs": -1, "unverified": 0, "fingerprint": "x", "cause": "labels_check"},
    {"below_inputs": True, "unverified": 0, "fingerprint": "x", "cause": "labels_check"},
    {"below_inputs": 0, "unverified": 0, "fingerprint": 5, "cause": "labels_check"},
    {"below_inputs": 0, "unverified": 0, "fingerprint": "x", "cause": "elsewhere"},
    {},
])
def test_an_unreadable_record_is_reported_and_never_shown_as_zero(label_harness, payload):
    h = label_harness
    with h.store() as store:
        store.append_event(build_event_log_record(event_type=LABEL_CHECK_EVENT, actor_type="system", payload=payload))
    _payload, check = derived_check(h)
    assert check["status"] == "skipped"
    assert check["details"]["unreadable"] is True
    assert "could not be read" in check["message"]
    assert "0 below" not in check["message"]


def test_fingerprint_follows_every_planner_input_and_nothing_else(label_harness):
    h = label_harness
    source = h.source()
    with h.store() as store:
        base = label_input_fingerprint(store.conn)
        assert label_input_fingerprint(store.conn) == base
        store.conn.execute("SET TIME ZONE 'Asia/Tokyo'")
        assert label_input_fingerprint(store.conn) == base
        store.conn.execute("UPDATE sources SET title = 'Renamed', author = 'Someone' WHERE id = %s::uuid", (str(source["id"]),))
        assert label_input_fingerprint(store.conn) == base, "columns the planner does not read must not move it"
        changes = (
            "UPDATE sources SET sensitivity = 'confidential' WHERE id = %s::uuid",
            "UPDATE sources SET domain = 'health' WHERE id = %s::uuid",
            "UPDATE sources SET metadata_json = metadata_json || '{\"project_scope\": [\"alpha\"]}'::jsonb WHERE id = %s::uuid",
        )
        seen = {base}
        for statement in changes:
            store.conn.execute(statement, (str(source["id"]),))
            fingerprint = label_input_fingerprint(store.conn)
            assert fingerprint not in seen
            seen.add(fingerprint)
