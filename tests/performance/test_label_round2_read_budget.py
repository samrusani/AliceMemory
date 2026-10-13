"""Varied parent and metadata fixtures expose per-row label work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_repair import label_gap_counts
from alicebot_api.vnext_label_guard import LabelGuard, label_read_request
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, DEFAULT_AGENT_SENSITIVITY
from tests.performance.read_budget_gate import (
    append_evidence,
    merge_base_checkout,
    over_merge_base,
    prelabels_action,
    prelabels_checkout,
)

USER = "11111111-1111-4111-8111-111111111111"


def seed_varied(store, *, postgres=False, count=5000, source_count=300, mixed=True, repaired=False, plain=False, all_hidden=False, unique_metadata=True):
    sources = [store.create_source({"source_type": "note", "title": "Synthetic budget input",
               "content_hash": str(uuid4()), "domain": "project",
               "sensitivity": "confidential" if all_hidden or i % 2 else "public"}) for i in range(source_count)]
    ids = [str(uuid4()) for _ in range(count)]
    values = []
    for i, row_id in enumerate(ids):
        parent_index = i % source_count
        source_id = str(sources[parent_index]["id"])
        metadata = {"project_scope": [], "project_floor": []}
        if unique_metadata:
            metadata["observation_index"] = i
        if not plain:
            metadata["source_id"] = source_id
            if mixed and i >= source_count:
                shape = (i // source_count) % 4
                if shape == 1:
                    metadata.update(workflow="project_auto_update")
                    metadata = with_derived_from(metadata, {"sources": [sources[parent_index]]})
                elif shape == 2:
                    metadata.update(candidate_kind="memory_consolidation", consolidation={"cluster_member_ids": [ids[parent_index]]})
                elif shape == 3:
                    metadata.update(discovered_by="vnext_weekly_synthesis")
                    metadata = with_derived_from(metadata, {"sources": [sources[parent_index]], "memories": [{"id": ids[parent_index]}]})
        sensitivity = "confidential" if repaired and not plain and (all_hidden or parent_index % 2) else "public"
        values.append((row_id, str(store.user_id) if not postgres else str(sources[0]["user_id"]),
                       "budget." + row_id, "synthetic budget observation " + str(i), json.dumps(metadata), sensitivity))
    if postgres:
        with store.conn.cursor() as cur:
            cur.executemany("INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,sensitivity,value,source_event_ids,status,domain) VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s,'{}','{}','active','project')", values)
            cur.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(%s::uuid,%s::uuid,%s::uuid,'memory.created','system','memory','{}')", [(str(uuid4()), values[0][1], row_id) for row_id in ids])
    else:
        store.conn.executemany("INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,sensitivity,value,source_event_ids,status,domain) VALUES(?,?,?,?,?,?,'{}','[]','active','project')", values)
        store.conn.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(?,?,?,'memory.created','system','memory','{}')", [(str(uuid4()), values[0][1], row_id) for row_id in ids])
    if repaired:
        assert label_gap_counts(store) == (0, 0), "post-repair budget must measure rows whose stored labels equal effective labels"
    keys = {}
    for profile in ("trusted_local_agent", "admin_agent"):
        _, keys[profile] = create_agent_key(store, user_id=store.user_id if not postgres else sources[0]["user_id"],
                                            agent_id="budget-" + profile, permission_profile=profile)
    return keys


def _open_probe(repo, backend, location, user, profile, key):
    script = Path(__file__).with_name("round2_budget_probe.py")
    return subprocess.Popen(
        [sys.executable, str(script), str(repo), backend, str(location), str(user), profile, key],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read_sample(process):
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def _profile_actions(processes, actions):
    """Ask each probe to profile every action after the timing samples, when a directory is set.

    The probe writes one file per action only when it is sent ``profile:<action>``. Closing the
    probe without that command leaves the directory empty.
    """

    if not os.environ.get("ALICE_ROUND2_PROFILE_DIR"):
        return
    for process in processes:
        for action in actions:
            process.stdin.write("profile:" + action + "\n")
            process.stdin.flush()
            _read_sample(process)


def _close_probe(process):
    if process.poll() is None:
        process.stdin.write("stop\n")
        process.stdin.flush()
    process.communicate(timeout=30)


def _samples(process, action, count):
    process.stdin.write(action + "\n")
    process.stdin.flush()
    _read_sample(process)
    walls, cpus = [], []
    for _ in range(count):
        process.stdin.write(action + "\n")
        process.stdin.flush()
        sample = _read_sample(process)
        walls.append(sample["wall"])
        cpus.append(sample["cpu"])
    return {"minimum_wall": min(walls), "minimum_cpu": min(cpus), "wall": walls, "cpu": cpus}


def _interleaved(base_process, head_process, actions, samples=5):
    measured = {"merge_base": {}, "head": {}}
    for action in actions:
        for process in (base_process, head_process):
            process.stdin.write(action + "\n")
            process.stdin.flush()
            _read_sample(process)
        arrays = [{"wall": [], "cpu": []} for _ in range(2)]
        for sample in range(samples):
            for index in (0, 1) if sample % 2 == 0 else (1, 0):
                process = (base_process, head_process)[index]
                process.stdin.write(action + "\n")
                process.stdin.flush()
                result = _read_sample(process)
                arrays[index]["wall"].append(result["wall"])
                arrays[index]["cpu"].append(result["cpu"])
        for index, revision in enumerate(("merge_base", "head")):
            measured[revision][action] = {
                "minimum_wall": min(arrays[index]["wall"]),
                "minimum_cpu": min(arrays[index]["cpu"]),
                "wall": arrays[index]["wall"],
                "cpu": arrays[index]["cpu"],
            }
    return measured


def assert_budgets(backend, location, user, keys):
    repo = Path(__file__).resolve().parents[2]
    merge_base = merge_base_checkout()
    prelabels = prelabels_checkout()
    actions = ("pack", "recall", "workspace", "dogfooding") if backend == "postgres" else ("pack", "recall")
    for profile, key in keys.items():
        processes = [_open_probe(checkout, backend, location, user, profile, key) for checkout in (merge_base, repo)]
        try:
            measured = _interleaved(processes[0], processes[1], actions)
            _profile_actions(processes, actions)
        finally:
            for process in processes:
                _close_probe(process)
        report = None
        if prelabels:
            process = _open_probe(prelabels, backend, location, user, profile, key)
            try:
                report = {"gated": False, "actions": {action: {} for action in actions}}
                for action in actions:
                    taken = _samples(process, action, 5)
                    for clock in ("minimum_wall", "minimum_cpu"):
                        report["actions"][action][clock] = prelabels_action(measured["head"][action][clock], taken[clock])
            finally:
                _close_probe(process)
        row = {
            "store": backend,
            "case": os.environ.get("ALICE_READ_BUDGET_CASE", "unspecified"),
            "profile": profile,
            "gate": "merge_base",
            "merge_base": measured["merge_base"],
            "head": measured["head"],
        }
        if report is not None:
            row["prelabels_report"] = report
        print(json.dumps(row), flush=True)
        append_evidence(row)
        for action in ("pack", "recall"):
            for clock in ("minimum_wall", "minimum_cpu"):
                assert not over_merge_base(measured["head"][action][clock], measured["merge_base"][action][clock]), (
                    action, clock, measured["head"], measured["merge_base"],
                )
        for action in ("workspace", "dogfooding") if backend == "postgres" else ():
            assert measured["head"][action]["minimum_wall"] <= 1, (action, measured["head"])


@label_read_request
def complete_count(store, ceiling):
    guard = LabelGuard.for_filters(store, (), ceiling)
    first = guard.readable_status_counts("memory")
    assert guard.readable_status_counts("memory") == first
    return first


@pytest.mark.parametrize("case,repaired", [("mixed", False), ("mixed", True), ("many-hidden", False), ("many-hidden", True), ("one-hidden", False), ("identical", False)])
def test_sqlite_varied_read_budget(tmp_path, monkeypatch, case, repaired):
    monkeypatch.setenv("ALICE_READ_BUDGET_CASE", case + ("-repaired" if repaired else ""))
    path = tmp_path / "round2.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        keys = seed_varied(SQLiteVNextStore(conn, USER), repaired=repaired,
                           source_count=1 if case in {"one-hidden", "identical"} else 3000 if case == "many-hidden" else 300,
                           all_hidden=case != "mixed", mixed=case == "mixed", unique_metadata=case != "identical")
        store = SQLiteVNextStore(conn, USER)
        for profile in keys:
            ceiling = ALL_SENSITIVITY if profile == "admin_agent" else DEFAULT_AGENT_SENSITIVITY
            expected = 5000 if profile == "admin_agent" else 2500 if case == "mixed" else 0
            walls, cpus = [], []
            for _ in range(3):
                wall_start, cpu_start = time.perf_counter(), time.process_time()
                assert complete_count(store, ceiling) == ({"active": expected} if expected else {})
                walls.append(time.perf_counter() - wall_start)
                cpus.append(time.process_time() - cpu_start)
            print(json.dumps({"count_case": case, "repaired": repaired, "profile": profile,
                              "minimum_wall": min(walls), "minimum_cpu": min(cpus), "admitted": expected}))
            assert min(walls) <= 1 and min(cpus) <= 1
    assert_budgets("sqlite", "sqlite:///" + str(path), USER, keys)


def test_round2_profiling_writes_a_profile_file_after_the_samples(tmp_path, monkeypatch):
    """With ALICE_ROUND2_PROFILE_DIR set, each measured action leaves a profile file.

    Mutation: drop the ``_profile_actions`` call. The directory stays empty and this test fails.
    """

    profile_dir = tmp_path / "profiles"
    monkeypatch.setenv("ALICE_ROUND2_PROFILE_DIR", str(profile_dir))
    monkeypatch.setenv("ALICE_READ_MERGE_BASE_CHECKOUT", str(Path(__file__).resolve().parents[2]))
    monkeypatch.delenv("ALICE_READ_MAIN_CHECKOUT", raising=False)
    path = tmp_path / "profile.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        _, key = create_agent_key(
            SQLiteVNextStore(conn, USER), user_id=USER, agent_id="budget-trusted", permission_profile="trusted_local_agent"
        )
    assert_budgets("sqlite", "sqlite:///" + str(path), USER, {"trusted_local_agent": key})
    files = sorted(profile_dir.glob("*.prof"))
    assert files, "profiling wrote no profile file"
    names = {item.name for item in files}
    assert any(name.endswith("-trusted_local_agent-pack.prof") for name in names), names
    assert any(name.endswith("-trusted_local_agent-recall.prof") for name in names), names
