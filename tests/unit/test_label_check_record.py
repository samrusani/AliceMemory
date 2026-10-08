"""The recorded label check: what it reads, how it parses a record, who may list it.

Mutations: let the fingerprint skip a column the planner reads; read a negative or
boolean count as a number; show an event of another type as the record; let a
restricted caller list the record; scan every node once per named weekly candidate
again, or let the last row with a key answer for it instead of the first.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import random
from types import SimpleNamespace
from uuid import UUID

import pytest

from alicebot_api import vnext_label_check_record as record
from alicebot_api import vnext_derived_labels as derived
from alicebot_api.vnext_derived_labels import SettledLabel, _node_label, _strings, canon_kind, with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_label_repair import INPUT_SELECTS_V3


def columns(select: str) -> list[str]:
    head = select.split(" FROM ")[0].removeprefix("SELECT ")
    return [part.strip() for part in head.split(",")]


def test_fingerprint_reads_every_column_the_planner_reads():
    assert set(record.FINGERPRINT_SELECTS) == set(INPUT_SELECTS_V3)
    for table, select in INPUT_SELECTS_V3.items():
        planner = columns(select)
        fingerprint = columns(record.FINGERPRINT_SELECTS[table])
        for column in planner:
            if column == "deleted_at":
                # Only whether it is set, so the row text does not depend on the session time zone.
                assert "deleted_at IS NOT NULL AS deleted" in fingerprint
            else:
                assert column in fingerprint, (table, column)
        assert len(fingerprint) == len(planner), (table, fingerprint)
        assert record.FINGERPRINT_SELECTS[table].endswith(f" FROM {table}")


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        assert params == (record.LABEL_CHECK_EVENT,)
        assert "ORDER BY occurred_at DESC, id DESC LIMIT 1" in query

    def fetchone(self):
        return self.row


def stored(payload, *, occurred_at=datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)):
    return SimpleNamespace(conn=SimpleNamespace(cursor=lambda: FakeCursor(
        {"occurred_at": occurred_at, "payload_json": payload})))


GOOD = {"below_inputs": 2, "unverified": 1, "fingerprint": "abc", "cause": "labels_repair"}


def test_a_sound_record_is_read_with_its_time():
    result = record.recorded_label_check(stored(GOOD))
    assert result == record.RecordedLabelCheck(
        readable=True, checked_at="2026-10-08T09:30:00Z", below_inputs=2, unverified=1,
        cause="labels_repair", fingerprint="abc")


def test_no_record_is_none():
    assert record.recorded_label_check(SimpleNamespace(conn=SimpleNamespace(cursor=lambda: FakeCursor(None)))) is None


@pytest.mark.parametrize("change", [
    {"below_inputs": -1}, {"below_inputs": True}, {"below_inputs": "0"}, {"below_inputs": 1.0},
    {"unverified": -3}, {"unverified": None}, {"fingerprint": 7}, {"fingerprint": None},
    {"cause": "other"}, {"cause": None},
])
def test_a_record_with_a_bad_field_is_unreadable_never_zero(change):
    result = record.recorded_label_check(stored({**GOOD, **change}))
    assert result.readable is False
    assert (result.below_inputs, result.unverified) == (0, 0)
    assert result.checked_at == "2026-10-08T09:30:00Z"


@pytest.mark.parametrize("payload", [None, [], "not json", "[1]", 5])
def test_a_record_that_is_not_an_object_is_unreadable(payload):
    assert record.recorded_label_check(stored(payload)).readable is False


def test_a_json_text_payload_is_decoded():
    assert record.recorded_label_check(stored('{"below_inputs": 0, "unverified": 0, "fingerprint": "f", "cause": "doctor"}')).readable


def test_only_known_causes_can_be_recorded():
    with pytest.raises(ValueError):
        record.record_label_check(SimpleNamespace(append_event=lambda event: None), below=0, unverified=0, fingerprint="f", cause="x")


def test_recording_appends_one_system_event_with_the_counts():
    events = []
    store = SimpleNamespace(append_event=events.append)
    record.record_label_check(store, below=3, unverified=2, fingerprint="f" * 64, cause="labels_check")
    (event,) = events
    assert event["event_type"] == "labels.checked" and event["actor_type"] == "system"
    assert event["target_type"] is None and event["target_id"] is None
    assert event["payload_json"] == {"below_inputs": 3, "unverified": 2, "fingerprint": "f" * 64, "cause": "labels_check"}


class EventRows:
    label_count_canonical_unique_ids = False

    def read_label_rows(self, kind, ids):
        return []


@pytest.mark.parametrize("active", [True, False])
def test_a_restricted_guard_never_lists_a_recorded_check_or_a_repair_event(active):
    events = [
        {"id": "1", "event_type": "labels.checked", "target_type": None, "target_id": None},
        {"id": "2", "event_type": "memory.labels_raised", "target_type": "memory", "target_id": None},
        {"id": "3", "event_type": "open_loop.labels_raised", "target_type": "weird", "target_id": None},
        {"id": "4", "event_type": "labels.checked_elsewhere", "target_type": None, "target_id": None},
        {"id": "5", "event_type": "scheduler.workflow_upserted", "target_type": "scheduler", "target_id": None},
    ]
    guard = LabelGuard(EventRows(), active=active, sensitivity_allowed=("public",))
    kept = {row["id"] for row in guard.admit_events(events)}
    assert kept == ({"4", "5"} if active else {"1", "2", "3", "4", "5"})


# ---- weekly candidates: one pass finds every named candidate's marker --------------------------------


def old_weekly_parent_deps(labels, own, nodes):
    """The per-candidate scan this replaced, kept as the oracle."""
    artifacts = [
        (label, derived._metadata(row))
        for label, row in nodes
        if label.kind == "artifact" and isinstance(derived._metadata(row).get("input_summary"), Mapping)
    ]
    for label, meta in artifacts:
        for candidate in _strings(meta.get("candidate_memory_ids")):
            candidate_key = ("memory", label.user_id, candidate)
            candidate_label = labels.get(candidate_key)
            if candidate_label is None or candidate_label.row_class != "aggregate":
                continue
            found = None
            for node_label, row in nodes:
                if node_label.key == candidate_key:
                    found = derived._metadata(row).get("discovered_by")
                    break
            if found != "vnext_weekly_synthesis":
                continue
            own.setdefault(candidate_key, set()).update(own.get(label.key, set()))


def weekly_world(seed: int, *, duplicate: bool):
    rng = random.Random(seed)
    sources = [{"kind": "source", "id": str(UUID(int=i + 1)), "user_id": "u", "domain": "project",
                "sensitivity": "public", "metadata_json": {}} for i in range(6)]
    memories = []
    for i in range(14):
        marker = rng.choice(["vnext_weekly_synthesis", "vnext_weekly_synthesis", "other", None])
        meta = {"observation": i}
        if marker:
            meta["discovered_by"] = marker
        memories.append({"kind": "memory", "id": str(UUID(int=100 + i)), "user_id": "u", "domain": "project",
                         "sensitivity": "public", "status": "candidate", "value": {}, "metadata_json": meta})
    if duplicate:
        twin = dict(memories[0])
        twin["metadata_json"] = {"observation": 99, "discovered_by": "other"}
        memories.insert(rng.randrange(len(memories)), twin)
    artifacts = []
    for i in range(10):
        picked = rng.sample(memories, rng.randrange(0, 4))
        named = [row["id"] for row in picked] + (["missing-" + str(i)] if i % 4 == 0 else [])
        inputs = rng.sample(sources, rng.randrange(1, 3))
        meta = with_derived_from({"workflow": "weekly_synthesis", "candidate_memory_ids": named,
                                  "input_summary": {"source_ids": [row["id"] for row in inputs]}}, {"sources": inputs})
        artifacts.append({"kind": "artifact", "id": str(UUID(int=500 + i)), "user_id": "u", "domain": "project",
                          "sensitivity": "public", "artifact_type": "weekly_synthesis", "metadata_json": meta})
    rows = sources + memories + artifacts
    rng.shuffle(rows)
    return rows


def run_both(seed, duplicate):
    rows = weekly_world(seed, duplicate=duplicate)
    prepared = [(_node_label(canon_kind(row["kind"]), row), row) for row in rows]
    labels = {label.key: label for label, _row in prepared}
    base = {label.key: {("source", label.user_id, label.stored_id)} for label, _row in prepared if label.derived}
    expected = {key: set(value) for key, value in base.items()}
    actual = {key: set(value) for key, value in base.items()}
    old_weekly_parent_deps(labels, expected, prepared)
    derived._weekly_parent_deps(labels, actual, prepared)
    return base, expected, actual


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("seed", range(30))
def test_single_pass_weekly_inputs_equal_the_per_candidate_scan(seed, duplicate):
    _base, expected, actual = run_both(seed, duplicate)
    assert actual == expected


@pytest.mark.parametrize("duplicate", [False, True])
def test_the_weekly_worlds_really_merge_artifact_inputs_into_candidates(duplicate):
    merged = 0
    for seed in range(30):
        base, _expected, actual = run_both(seed, duplicate)
        merged += sum(1 for key in actual if actual[key] != base[key])
    assert merged >= 30, merged


def test_a_scan_of_a_large_graph_visits_each_node_once_not_once_per_candidate(monkeypatch):
    """Thirty named candidates over 2,000 rows must not read 60,000 node keys."""
    rows = weekly_world(7, duplicate=False)
    filler = [{"kind": "source", "id": str(UUID(int=10_000 + i)), "user_id": "u", "domain": "project",
               "sensitivity": "public", "metadata_json": {}} for i in range(2000)]
    prepared = [(_node_label(canon_kind(row["kind"]), row), row) for row in rows + filler]
    labels = {label.key: label for label, _row in prepared}
    reads = 0
    original = SettledLabel.key

    def counting(self):
        nonlocal reads
        reads += 1
        return original.fget(self)

    monkeypatch.setattr(SettledLabel, "key", property(counting))
    derived._weekly_parent_deps(labels, {}, prepared)
    assert reads <= 3 * len(prepared), reads


# ---- the guides say what the workspace shows and which commands record --------------------------------


def guide(path):
    from pathlib import Path

    return " ".join((Path(__file__).resolve().parents[2] / path).read_text().split())


def test_the_doctor_guide_names_the_recorded_check_its_causes_and_its_limits():
    """Mutations: delete the recorded-check section, the no-record wording or the live-route sentence."""
    doctor = guide("docs/alpha/doctor.md")
    for phrase in (
        "### The workspace shows the last full labels check",
        "`alicebot vnext doctor`), `alicebot vnext labels check` and `alicebot vnext labels repair` run the full check and record its result",
        "`derived labels: N below their inputs, M unverified (recorded by labels check at <time>)`",
        "The label inputs have changed since this check; run alicebot vnext labels check for a current result.",
        "`derived labels: no recorded check; run alicebot vnext labels check`",
        "A record the workspace cannot read is reported as unreadable, never as zero rows.",
        "an event named `labels.checked` in the audit log",
        "Restricted keys never list it",
        "`GET /v0/vnext/doctor` and `POST /v0/vnext/doctor/run` stay live full checks and do not record.",
        "If the record cannot be written, the command prints `result was not recorded`",
    ):
        assert phrase in doctor, phrase
    backup = guide("docs/alpha/backup-and-restore.md")
    assert "appends one `labels.checked` event with the counts, a digest of the rows it read and no row text" in backup
    assert "the check changes no label" in backup
