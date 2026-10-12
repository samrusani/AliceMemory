"""Resume and the grounding probe stay within a budget of the baseline when many newer rows are hidden from the caller.

Unreleased (on main, not in v0.20.0). ``alice_resume`` and the grounding probe of the context pack read past the rows a caller's
limits hide by the labels of their inputs. A vault with 10,000 such rows newer than the first row the caller may read must not
make them slow: a read judges at most ``READABLE_SCAN_CAP`` rows, whatever the vault holds. And a vault with no such row must
not make them slower than they were: a read that looks past hidden rows pays for it only when there are some.

The paired measurement is the one of the read budgets next to this file: a persistent process per revision (this checkout and the
checkout named by ``ALICE_READ_MAIN_CHECKOUT``, which the test requires) answers the same action in turn on the same data, ten
samples each, and the minimum wall and CPU time of this revision must be at most twice the baseline's plus a tenth of a second
(plus 0.03 s for the case with no hidden row, where the two should be equal). A fast read that returns the wrong answer passes
no budget: what this revision returned is checked too.

Cases, each with twelve readable decisions older than the hidden rows, and an event for each row:

* ``derived``: 10,000 hidden rows whose source is in the health domain. The cap is reached: no decision, no recent change, and the
  grounding probe gave up and reads the name as supported.
* ``unproven``: 10,000 hidden rows that list an input that does not exist, which the check reads as unverified. The same answers.
* ``under``: 100 hidden rows, fewer than the cap. The key is shown the newest readable decision and the recent changes of a
  vault without them, and the grounding probe, which looked at every hidden mention, lists both names.
* ``sources`` (PostgreSQL only): 200,000 sources, one in ten in the health domain, and no hidden row. A read that scanned the
  sources table for every list would cost many times the baseline here and nothing in the other cases.

The grounding probe is not measured on SQLite: its full-text query scores every hidden match, which costs about 2.5 ms a match
there at every revision that applies the label proof to it, and the baseline of CI is older than that.

The paired checkout may be an older revision than the one that judges rows by their inputs (the CI baseline is), and such a
revision shows the hidden decision. Only this revision's answers are checked; the other's time is the measurement.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP

USER = "11111111-1111-4111-8111-111111111111"
READABLE_DECISIONS = 12
HIDDEN_ROWS = 10_000
# The hidden rows that mention the name the grounding probe looks for. The full-text search scores every match before it returns
# the first, so the probe is measured over a thousand of them and the other rows are hidden decisions that do not match.
HIDDEN_MENTIONS = 1_000
BULK_SOURCES = 200_000
# case: (hidden rows, shape of the rows)
CASES = {"derived": (HIDDEN_ROWS, "derived"), "unproven": (HIDDEN_ROWS, "unproven"), "under": (100, "derived"), "sources": (0, "derived")}
# The domains a key that may not read the restricted ones (``read_only_agent``) is given.
READ_ONLY_DOMAINS = ["professional", "personal", "learning", "relationship", "project", "agent_run", "system", "unknown"]
ACTIONS = ("resume", "resume_changes", "resume_query", "grounding")
SQLITE_ACTIONS = tuple(action for action in ACTIONS if action != "grounding")
SAMPLES = 10


def seed_hidden_rows(store, *, postgres, hidden=HIDDEN_ROWS, shape="derived"):
    """Twelve readable decisions, then ``hidden`` newer rows that the key may not read, each with a ``memory.created`` event.

    The rows are written with the SQL of the grid seeds next to this file, so they carry no insert floor: their stored labels are
    public and project, and only their inputs put them out of the key's reach. The timestamps are explicit so that the order is
    the order of the loop, whatever the clock of the transaction.
    """

    parent = store.create_source({"source_type": "note", "title": "Synthetic hidden parent", "content_hash": str(uuid4()),
                                  "domain": "health", "sensitivity": "public", "metadata_json": {}})
    user = str(parent["user_id"])
    rows, events = [], []
    for index in range(READABLE_DECISIONS + hidden):
        visible = index < READABLE_DECISIONS
        row_id = str(uuid4())
        source = None if visible else (str(parent["id"]) if shape == "derived" else str(uuid4()))
        metadata = {} if visible else {"source_id": source}
        mention = READABLE_DECISIONS <= index < READABLE_DECISIONS + HIDDEN_MENTIONS
        text = f"visible decision {index}" if visible else (f"Zorblax hidden decision {index}" if mention else f"hidden decision {index}")
        stamp = f"2026-0{1 if visible else 2}-01T{index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}Z"
        rows.append((row_id, user, "budget.resume." + row_id, text, json.dumps(metadata), stamp, stamp))
        events.append((str(uuid4()), user, row_id, stamp))
    if postgres:
        with store.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity,memory_type,created_at,updated_at) "
                "VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}','{}','active','project','public','decision',%s::timestamptz,%s::timestamptz)", rows)
            cur.executemany(
                "INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json,occurred_at) "
                "VALUES(%s::uuid,%s::uuid,%s::uuid,'memory.created','system','memory','{}',%s::timestamptz)", events)
    else:
        store.conn.executemany(
            "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity,memory_type,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'{}','[]','active','project','public','decision',?,?)", rows)
        store.conn.executemany(
            "INSERT INTO event_log(id,user_id,target_id,event_type,actor_type,target_type,payload_json,occurred_at) "
            "VALUES(?,?,?,'memory.created','system','memory','{}',?)", events)


def paired_resume_budgets(backend, location, user, key, *, case, assert_budget=True, samples=SAMPLES, actions=ACTIONS):
    assert samples >= 10
    repo = Path(__file__).resolve().parents[2]
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    assert main, "the resume budgets require the paired baseline checkout (ALICE_READ_MAIN_CHECKOUT)"
    slack = 0.03 if case == "sources" else 0.1
    script = Path(__file__).with_name("resume_budget_probe.py")
    processes = [subprocess.Popen([sys.executable, str(script), str(checkout), backend, location, str(user), key, json.dumps(READ_ONLY_DOMAINS)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for checkout in (main, repo)]
    failures, rows = [], []
    try:
        for action in actions:
            arrays = [{"wall": [], "cpu": [], "returned": []} for _ in processes]
            for process in processes:  # one unmeasured round: imports and connections
                process.stdin.write(action + "\n")
                process.stdin.flush()
                assert process.stdout.readline(), process.stderr.read()
            for sample in range(samples):
                for index in (0, 1) if sample % 2 == 0 else (1, 0):
                    process = processes[index]
                    process.stdin.write(action + "\n")
                    process.stdin.flush()
                    line = process.stdout.readline()
                    assert line, process.stderr.read()
                    result = json.loads(line)
                    arrays[index]["wall"].append(result.pop("wall"))
                    arrays[index]["cpu"].append(result.pop("cpu"))
                    arrays[index]["returned"].append(result)
            measured = {revision: {"minimum_wall": min(a["wall"]), "minimum_cpu": min(a["cpu"]), "returned": a["returned"][-1]}
                        for revision, a in zip(("main", "head"), arrays)}
            row = {"store": backend, "case": case, "hidden": CASES[case][0], "action": action, "samples": samples, **measured}
            rows.append(row)
            print(json.dumps(row), flush=True)
            evidence = os.environ.get("ALICE_READ_BUDGET_EVIDENCE")
            if evidence:
                Path(evidence).parent.mkdir(parents=True, exist_ok=True)
                with Path(evidence).open("a") as output:
                    output.write(json.dumps(row) + "\n")
            for clock in ("minimum_wall", "minimum_cpu"):
                if measured["head"][clock] > 2 * measured["main"][clock] + slack:
                    failures.append((action, clock, row))
    finally:
        for process in processes:
            if process.poll() is None:
                process.stdin.write("stop\n")
                process.stdin.flush()
            process.communicate(timeout=30)
    wrong = returned_answers_wrong(case, rows)
    if assert_budget:
        assert not failures, failures
        assert not wrong, wrong
    return failures + wrong


def returned_answers_wrong(case, rows):
    """What this revision returned. A read that is fast and empty passes no budget.

    Only this revision's answers are held to a value. The paired checkout is whatever revision the caller names, and an older
    one does not judge a row by its inputs: it shows the hidden decision where a later one shows none. Its time is the
    measurement, and what it returned is recorded with the row of evidence.

    With the cap reached (``derived`` and ``unproven``) this revision shows no decision and no recent change, and its grounding
    probe gave up and reads the name as supported. With fewer hidden rows than the cap (``under``) and with none (``sources``)
    it shows the newest readable decision and some recent changes, and the grounding block lists both names.
    """

    capped = CASES[case][0] >= READABLE_SCAN_CAP
    wrong = []
    for row in rows:
        head, action = row["head"]["returned"], row["action"]
        if action == "grounding":
            if sorted(head["unsupported"]) != (["Quuxington"] if capped else ["Quuxington", "Zorblax"]):
                wrong.append((action, "head", head))
        elif action in {"resume", "resume_changes"}:
            if head["decision"] != (None if capped else f"visible decision {READABLE_DECISIONS - 1}"):
                wrong.append((action, "head", head))
            if (head["changes"] == 0) != capped:
                wrong.append((action, "head changes", head))
        elif action == "resume_query" and head["decision"] is not None:
            wrong.append((action, "head", head))
    return wrong


@pytest.mark.parametrize("case", [name for name in CASES if name != "sources"])
def test_sqlite_resume_stays_within_the_budget_of_the_baseline_with_many_hidden_rows(tmp_path, case):
    path = tmp_path / "resume-budget.db"
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    hidden, shape = CASES[case]
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        seed_hidden_rows(store, postgres=False, hidden=hidden, shape=shape)
        _record, key = create_agent_key(store, user_id=USER, agent_id="resume-budget", permission_profile="read_only_agent")
    paired_resume_budgets("sqlite", "sqlite:///" + str(path), USER, key, case=case, actions=SQLITE_ACTIONS)
