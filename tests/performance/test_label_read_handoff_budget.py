"""Read budgets use 5,000 copies and preserve complete effective admission."""
import json
import time
import os
from pathlib import Path
from uuid import uuid4

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_label_guard import LabelGuard, label_read_request
from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
from alicebot_api.vnext_source_fence import SourceReadFence

USER = "11111111-1111-4111-8111-111111111111"
CEILING = ("public", "internal", "private", "unknown")


def seed_copies(store, count=5000, *, postgres=False):
    source = store.create_source({"source_type": "note", "title": "Private input", "content_hash": str(uuid4()),
                                  "domain": "project", "sensitivity": "confidential"})
    meta = json.dumps({"source_id": str(source["id"])})
    ids = [str(uuid4()) for _ in range(count)]
    values = [(row_id, USER if not postgres else str(source["user_id"]), "copy." + row_id, "synthetic budget observation", meta) for row_id in ids]
    if postgres:
        sql = "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}','{}','active','project','public')"
        with store.conn.cursor() as cur:
            cur.executemany(sql, values)
    else:
        store.conn.executemany("INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) VALUES(?,?,?,?,?,'{}','[]','active','project','public')", values)
    events = [(str(uuid4()), str(source["user_id"]), row_id) for row_id in ids]
    if postgres:
        with store.conn.cursor() as cur:
            cur.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(%s::uuid,%s::uuid,%s::uuid,'memory.created','system','memory','{}')", events)
    else:
        store.conn.executemany("INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json) VALUES(?,?,?,'memory.created','system','memory','{}')", events)
    return source, ids


def run_pack(store):
    service = VNextRetrievalService(store, embedding_provider=None, reranker_provider=None)
    identity = AgentIdentity(agent_id="budget", permission_profile="trusted_local_agent")
    return service.compile_context_pack(VNextRetrievalRequest(query="synthetic budget observation", sensitivity_allowed=CEILING),
                                        source_fence=SourceReadFence.for_identity(identity))


@label_read_request
def count_twice(store):
    first = LabelGuard.for_filters(store, (), CEILING).readable_status_counts("memory")
    second = LabelGuard.for_filters(store, (), CEILING).readable_status_counts("memory")
    assert first == second == {}


def test_sqlite_five_thousand_derived_rows_budget(tmp_path):
    path = tmp_path / "vault.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source, ids = seed_copies(store)
        started = time.perf_counter()
        count_twice(store)
        counted = time.perf_counter() - started
        assert counted <= 1.0, counted
        started = time.perf_counter()
        pack = run_pack(store)
        elapsed = time.perf_counter() - started
        assert not any(row_id in str(pack) for row_id in ids)
        print(f"SQLite 5000: counted={counted:.4f}s pack={elapsed:.4f}s")
        assert elapsed <= 0.15, elapsed
    from tests.integration.test_label_read_handoff_budget_postgres import probe
    head = probe(Path(__file__).resolve().parents[2], "sqlite", path, USER)
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    if main:
        baseline = probe(main, "sqlite", path, USER)
        print("SQLite head=" + json.dumps(head) + " main=" + json.dumps(baseline))
        for action in ("pack", "recall"):
            assert head[action]["median"] <= 2 * baseline[action]["median"] + .1, (action, head, baseline)
    else:
        assert head["pack"]["median"] <= .15, head
        assert head["recall"]["median"] <= .15, head
