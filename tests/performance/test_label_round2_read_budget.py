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

USER = "11111111-1111-4111-8111-111111111111"


def seed_varied(store, *, postgres=False, count=5000, source_count=300, mixed=True, repaired=False, plain=False, all_hidden=False):
    sources = [store.create_source({"source_type": "note", "title": "Synthetic budget input",
               "content_hash": str(uuid4()), "domain": "project",
               "sensitivity": "confidential" if all_hidden or i % 2 else "public"}) for i in range(source_count)]
    ids = [str(uuid4()) for _ in range(count)]
    values = []
    for i, row_id in enumerate(ids):
        parent_index = i % source_count
        source_id = str(sources[parent_index]["id"])
        metadata = {"observation_index": i, "project_scope": [], "project_floor": []}
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


def probe(repo, backend, location, user, profile, key):
    script = Path(__file__).with_name("round2_budget_probe.py")
    completed = subprocess.run([sys.executable, str(script), str(repo), backend, str(location), str(user), profile, key],
                               capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])["times"]


def assert_budgets(backend, location, user, keys):
    repo = Path(__file__).resolve().parents[2]
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    assert main, "round-two budgets require the exact paired main checkout"
    for profile, key in keys.items():
        baseline = probe(main, backend, location, user, profile, key)
        head = probe(repo, backend, location, user, profile, key)
        print(json.dumps({"store": backend, "profile": profile, "main": baseline, "head": head}))
        for action in ("pack", "recall"):
            for clock in ("minimum_wall", "minimum_cpu"):
                assert head[action][clock] <= 2 * baseline[action][clock] + .1, (action, clock, head, baseline)
        for action in ("workspace", "dogfooding") if backend == "postgres" else ():
            assert head[action]["minimum_wall"] <= 1, (action, head)


@label_read_request
def complete_count(store, ceiling):
    guard = LabelGuard.for_filters(store, (), ceiling)
    first = guard.readable_status_counts("memory")
    assert guard.readable_status_counts("memory") == first
    return first


@pytest.mark.parametrize("case,repaired", [("mixed", False), ("mixed", True), ("many-hidden", False), ("many-hidden", True), ("one-hidden", False)])
def test_sqlite_varied_read_budget(tmp_path, case, repaired):
    path = tmp_path / "round2.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        keys = seed_varied(SQLiteVNextStore(conn, USER), repaired=repaired,
                           source_count=1 if case == "one-hidden" else 3000 if case == "many-hidden" else 300,
                           all_hidden=case != "mixed", mixed=case == "mixed")
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
