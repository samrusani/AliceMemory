#!/usr/bin/env python3
"""Measure actual capture and relabel work in fresh disposable PostgreSQL databases."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import statistics
import time
from uuid import uuid4

from alembic import command

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import _create_role_separated_database, _drop_database


def capture(url, repetitions):
    samples = []
    for repeat in range(repetitions + 1):
        user_id = uuid4()
        with user_connection(url, user_id) as conn:
            ContinuityStore(conn).create_user(user_id, f"capture-{user_id}@example.invalid", "Capture")
            store = PostgresVNextStore(conn)
            text = "\n".join(
                f"Decision: Synthetic item {index} is assigned to synthetic route {index}." for index in range(200)
            )
            start = time.perf_counter()
            profile = cProfile.Profile() if os.getenv("ALICE_BUDGET_PROFILE") else None
            if profile:
                profile.enable()
            result = VNextCaptureService(store).capture_source(
                SourceCaptureInput(
                    source_type="manual_text",
                    title="Synthetic capture",
                    raw_text=text,
                    domain="project",
                    sensitivity="public",
                )
            )
        elapsed = time.perf_counter() - start
        if profile:
            profile.disable()
            profile.dump_stats(os.environ["ALICE_BUDGET_PROFILE"])
            pstats.Stats(profile).sort_stats("cumulative").print_stats(45)
        assert result.candidate_memory_count == 200, result.to_record()
        with user_connection(url, user_id) as conn:
            assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 200
        if repeat:
            samples.append(elapsed)
    return {
        "facts": 200,
        "warmup_runs": 1,
        "repetitions": repetitions,
        "seconds": samples,
        "median_seconds": statistics.median(samples),
        "timing_includes_commit": True,
        "fixture_validation_timed": False,
    }


def relabel(url, repetitions, dependants, *, state=None, prepare=False):
    from alicebot_api.vnext_derived_labels import with_derived_from

    user_id = uuid4() if state is None else state["user_id"]
    if state is None:
        with user_connection(url, user_id) as conn:
            ContinuityStore(conn).create_user(user_id, f"relabel-{user_id}@example.invalid", "Relabel")
            store = PostgresVNextStore(conn)
            source = store.create_source(
                {
                    "source_type": "manual_text",
                    "title": "Synthetic input",
                    "content_hash": str(uuid4()),
                    "domain": "project",
                    "sensitivity": "public",
                }
            )
            source_id = str(source["id"])
            conn.execute(
                """INSERT INTO memories(id,user_id,memory_key,value,title,canonical_text,status,domain,sensitivity,source_event_ids,metadata_json)
            SELECT gen_random_uuid(),app.current_user_id(),'scale.'||n,'{}'::jsonb,'Synthetic row '||n,'Synthetic scale row '||n,
                   'active','project','public','[]'::jsonb,CASE WHEN n<=%s THEN jsonb_build_object('source_id',%s::text) ELSE '{}'::jsonb END
            FROM generate_series(1,50000) n""",
                (dependants, source_id),
            )
            record = with_derived_from({"workflow": "daily_brief"}, {"sources": [source]})
            conn.execute(
                """INSERT INTO generated_artifacts(id,user_id,artifact_type,title,content_markdown,status,domain,sensitivity,generated_by,metadata_json)
            SELECT gen_random_uuid(),app.current_user_id(),'daily_brief','Synthetic artifact '||n,'Synthetic scale artifact '||n,
                   'needs_review','project','public','system',CASE WHEN n<=%s THEN %s::jsonb ELSE %s::jsonb END
            FROM generate_series(1,2000) n""",
                (dependants, json.dumps(record), json.dumps(with_derived_from({"workflow": "daily_brief"}, {}))),
            )
    else:
        source_id = state["source_id"]
    if prepare:
        return {"user_id": str(user_id), "source_id": source_id, "dependants": dependants}
    samples = []
    # Commit is timed. Restore only this synthetic fixture outside each timed operation.
    for repeat in range(repetitions + 1):
        with user_connection(url, user_id) as conn:
            store = PostgresVNextStore(conn)
            before_events = conn.execute(
                "SELECT count(*) AS n FROM event_log WHERE event_type LIKE '%%.labels_raised'"
            ).fetchone()["n"]
            start = time.perf_counter()
            profile = cProfile.Profile() if os.getenv("ALICE_BUDGET_PROFILE") else None
            if profile:
                profile.enable()
            store.update_source(source_id=source_id, patch={"sensitivity": "confidential"})
        elapsed = time.perf_counter() - start
        if profile:
            profile.disable()
            profile.dump_stats(os.environ["ALICE_BUDGET_PROFILE"])
            pstats.Stats(profile).sort_stats("cumulative").print_stats(45)
        with user_connection(url, user_id) as conn:
            assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 50000
            assert conn.execute("SELECT count(*) AS n FROM generated_artifacts").fetchone()["n"] == 2000
            assert (
                conn.execute("SELECT count(*) AS n FROM memories WHERE sensitivity='confidential'").fetchone()["n"]
                == dependants
            )
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM generated_artifacts WHERE sensitivity='confidential'"
                ).fetchone()["n"]
                == dependants
            )
            assert (
                conn.execute("SELECT sensitivity FROM sources WHERE id=%s", (source_id,)).fetchone()["sensitivity"]
                == "confidential"
            )
            assert (
                conn.execute("SELECT count(*) AS n FROM event_log WHERE event_type LIKE '%%.labels_raised'").fetchone()[
                    "n"
                ]
                - before_events
                == 2 * dependants
            )
            conn.execute("UPDATE sources SET sensitivity='public' WHERE id=%s", (source_id,))
            conn.execute("UPDATE memories SET sensitivity='public' WHERE sensitivity='confidential'")
            conn.execute("UPDATE generated_artifacts SET sensitivity='public' WHERE sensitivity='confidential'")
        if repeat:
            samples.append(elapsed)
    return {
        "sources": 1,
        "memories": 50000,
        "artifacts": 2000,
        "dependent_memories": dependants,
        "dependent_artifacts": dependants,
        "warmup_runs": 1,
        "repetitions": repetitions,
        "seconds": samples,
        "median_seconds": statistics.median(samples),
        "budget_seconds": 3,
        "label_events_per_run": 2 * dependants,
        "timing_includes_commit": True,
        "fixture_validation_and_reset_timed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("capture", "relabel"), required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--dependants", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--prepared", type=Path)
    args = parser.parse_args()
    prepared = json.loads(args.prepared.read_text()) if args.prepared else None
    name = prepared["database_name"] if prepared else "alice_label_perf_" + uuid4().hex[:12]
    urls = prepared["urls"] if prepared else _create_role_separated_database(name)
    retained = False
    try:
        if prepared is None:
            command.upgrade(make_alembic_config(urls["admin"]), "head")
        if args.prepare:
            state = relabel(urls["app"], 0, args.dependants, prepare=True) if args.mode == "relabel" else None
            args.prepare.write_text(json.dumps({"database_name": name, "urls": urls, "state": state}, indent=2) + "\n")
            retained = True
            print("Synthetic scale fixture prepared")
            return
        record = (
            capture(urls["app"], args.repetitions)
            if args.mode == "capture"
            else relabel(
                urls["app"],
                args.repetitions,
                prepared["state"]["dependants"] if prepared else args.dependants,
                state=prepared["state"] if prepared else None,
            )
        )
        args.output.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record))
    finally:
        if not retained:
            _drop_database(name)


if __name__ == "__main__":
    main()
