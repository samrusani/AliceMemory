"""Exact random-parent grid requested by the third external handoff."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from, is_derived, dependencies_of
from alicebot_api.vnext_label_repair import label_gap_counts, relabel_labels_sqlite

USER = "11111111-1111-4111-8111-111111111111"
CASES = ("half-hidden", "all-hidden", "all-visible", "deeper-ancestry", "extra-rows")
SOURCE_COUNTS = (300, 1000, 3000)


def seed_grid(store, *, postgres=False, count=5000, source_count=300, case="half-hidden", identical=False, provision_keys=True):
    rng = random.Random(20261007)
    sources = [store.create_source({"source_type": "note", "title": "Synthetic budget input",
               "content_hash": str(uuid4()), "domain": "project",
               "sensitivity": "confidential" if case == "all-hidden" or (case != "all-visible" and i % 2) else "public"})
               for i in range(source_count)]
    user = str(sources[0]["user_id"])
    ids = [str(uuid4()) for _ in range(count)]
    shapes = ["copy"] * (count * 30 // 100) + ["report"] * (count * 25 // 100) + ["consolidation"] * (count * 25 // 100)
    shapes += ["weekly"] * (count - len(shapes))
    rng.shuffle(shapes)
    first_copy = shapes.index("copy")
    shapes[0], shapes[first_copy] = shapes[first_copy], shapes[0]
    copies = [ids[i] for i, shape in enumerate(shapes) if shape == "copy"]
    values = []
    for i, row_id in enumerate(ids):
        metadata = {"project_scope": [], "project_floor": []}
        if not identical:
            metadata["observation_index"] = i
        selected_sources = rng.sample(sources, min(source_count, 1 if shapes[i] == "copy" else rng.randint(1, 3)))
        shape = "copy" if identical else shapes[i]
        if shape == "copy":
            metadata["source_id"] = str(sources[0]["id"] if identical else selected_sources[0]["id"])
        elif shape == "report":
            metadata["workflow"] = "project_auto_update"
            metadata = with_derived_from(metadata, {"sources": selected_sources})
        elif shape == "consolidation":
            pool = ids[:i] if case == "deeper-ancestry" else copies
            members = rng.sample(pool, min(len(pool), rng.randint(1, 3)))
            metadata.update(candidate_kind="memory_consolidation", consolidation={"cluster_member_ids": members})
        else:
            metadata["discovered_by"] = "vnext_weekly_synthesis"
            metadata = with_derived_from(metadata, {"sources": selected_sources,
                "memories": [{"id": item} for item in rng.sample(copies, rng.randint(1, 2))]})
        values.append((row_id, user, "budget." + row_id, "synthetic budget observation " + str(i), json.dumps(metadata)))
    if postgres:
        with store.conn.cursor() as cur:
            cur.executemany("INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}','{}','active','project','public')", values)
            cur.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(%s::uuid,%s::uuid,%s::uuid,'memory.created','system','memory','{}')", [(str(uuid4()), user, row_id) for row_id in ids])
    else:
        store.conn.executemany("INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) VALUES(?,?,?,?,?,'{}','[]','active','project','public')", values)
        store.conn.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(?,?,?,'memory.created','system','memory','{}')", [(str(uuid4()), user, row_id) for row_id in ids])
    if case == "extra-rows":
        for i in range(400):
            source = rng.choice(sources)
            loop = store.create_open_loop({"title": "Synthetic loop " + str(i), "status": "open", "source_id": str(source["id"]),
                "domain": "project", "sensitivity": "public", "metadata_json": with_derived_from({"observation_index": i, "discovered_by": "vnext_project_open_loop_extraction"}, {"sources": [source]})})
            assert is_derived("open_loop", loop)
            assert ("source", str(source["id"])) in dependencies_of("open_loop", loop)
        if postgres:
            for i in range(500):
                store.create_artifact({"artifact_type": "weekly_synthesis", "title": "Synthetic artifact " + str(i),
                    "content_markdown": "Synthetic report", "domain": "project", "sensitivity": "public",
                    "metadata_json": with_derived_from({"observation_index": i}, {"sources": rng.sample(sources, 2)})})
    return budget_keys(store, user_id=user) if provision_keys else {}


def budget_keys(store, *, user_id=None):
    keys = {}
    for profile in ("trusted_local_agent", "admin_agent"):
        _, keys[profile] = create_agent_key(store, user_id=user_id or store.user_id,
                                            agent_id="budget-" + profile, permission_profile=profile)
    return keys


def repair_fixture(backend, location, user):
    if backend == "sqlite":
        with sqlite_user_connection(location.removeprefix("sqlite:///"), user) as conn:
            applied = relabel_labels_sqlite(conn, explicit=True)
            assert label_gap_counts(SQLiteVNextStore(conn, user)) == (0, 0)
    else:
        from alicebot_api.cli.labels import _run_vnext_labels_repair, _run_vnext_labels_check
        ctx = SimpleNamespace(database_url=location, user_id=user)
        applied = _run_vnext_labels_repair(ctx, None)
        assert _run_vnext_labels_check(ctx, None) == "below_inputs 0"
    return applied


def paired_budgets(backend, location, user, keys, *, case, source_count, repaired, samples=10, assert_budget=True):
    assert samples >= 10
    repo = Path(__file__).resolve().parents[2]
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    assert main, "round-three budgets require the exact paired main checkout"
    script = Path(__file__).with_name("round3_budget_probe.py")
    revisions = {name: subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                 for name, checkout in (("main", main), ("head", repo))}
    failures = []
    for profile, key in keys.items():
        processes = [subprocess.Popen([sys.executable, str(script), str(checkout), backend, location, str(user), profile, key],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for checkout in (main, repo)]
        try:
            measurements = {revision: {} for revision in ("main", "head")}
            for action in ("pack", "recall", "workspace", "dogfooding") if backend == "postgres" else ("pack", "recall"):
                for process in processes:
                    process.stdin.write(action + "\n")
                    process.stdin.flush()
                    line = process.stdout.readline()
                    assert line, process.stderr.read()
                arrays = [{"wall": [], "cpu": [], "returned_memories": []} for _ in processes]
                for sample in range(samples):
                    for index in (0, 1) if sample % 2 == 0 else (1, 0):
                        process = processes[index]
                        process.stdin.write(action + "\n")
                        process.stdin.flush()
                        line = process.stdout.readline()
                        assert line, process.stderr.read()
                        result = json.loads(line)
                        for clock in ("wall", "cpu"):
                            arrays[index][clock].append(result[clock])
                        if "memories" in result:
                            arrays[index]["returned_memories"].append(result["memories"])
                for index, revision in enumerate(("main", "head")):
                    measurements[revision][action] = {**arrays[index], "minimum_wall": min(arrays[index]["wall"]),
                                                     "minimum_cpu": min(arrays[index]["cpu"])}
            row = {"store": backend, "case": case, "source_count": source_count, "repaired": repaired,
                   "profile": profile, "revisions": revisions, "samples": samples, "seed": 20261007,
                   "memories": 5000, "mix": ({"source_copy": 5000} if case == "identical-copies" else {"source_copy": 1500, "stamped_report": 1250, "consolidation": 1250, "weekly": 1000}),
                   "artifacts": 500 if backend == "postgres" and case == "extra-rows" else 0,
                   "derived_loops": 400 if case == "extra-rows" else 0, "real_repair_zero_check": repaired, **measurements}
            print(json.dumps(row), flush=True)
            for action in ("pack", "recall"):
                for clock in ("minimum_wall", "minimum_cpu"):
                    if measurements["head"][action][clock] > 2 * measurements["main"][action][clock] + .1:
                        failures.append((profile, action, clock, row))
            if backend == "postgres":
                for action in ("workspace", "dogfooding"):
                    if measurements["head"][action]["minimum_wall"] > 1:
                        failures.append((profile, action, "minimum_wall", row))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                process.communicate(timeout=30)
    if assert_budget:
        assert not failures, failures
    return failures


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("source_count", SOURCE_COUNTS)
@pytest.mark.parametrize("repaired", (False, True))
def test_sqlite_round3_random_read_budgets(tmp_path, case, source_count, repaired):
    path = tmp_path / "round3.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        seed_grid(SQLiteVNextStore(conn, USER), case=case, source_count=source_count, provision_keys=False)
    location = "sqlite:///" + str(path)
    if repaired:
        repair_fixture("sqlite", location, USER)
    failures = paired_budgets("sqlite", location, USER, {"keyless_agent_id": ""}, case=case, source_count=source_count, repaired=repaired, assert_budget=False)
    with sqlite_user_connection(path, USER) as conn:
        keys = budget_keys(SQLiteVNextStore(conn, USER))
    failures.extend(paired_budgets("sqlite", location, USER, keys, case=case, source_count=source_count, repaired=repaired, assert_budget=False))
    assert not failures, failures


def test_sqlite_round3_identical_copy_control(tmp_path):
    path = tmp_path / "identical.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        keys = seed_grid(SQLiteVNextStore(conn, USER), source_count=1, identical=True)
    paired_budgets("sqlite", "sqlite:///" + str(path), USER, keys, case="identical-copies", source_count=1, repaired=False)
