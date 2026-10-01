"""Time alice_recall and alice_context_pack on a synthetic SQLite vault.

Recall and the context pack label every packed source whose derived memory was
later corrected. That label needs the memories that reference the source. v0.19.0
asked the store once per packed source, and each ask scans the memories table
(JSON parse per row). This script measures what that costs and how many
statements a call runs.

Two steps, both offline, no network, no model, no embeddings:

    PYTHONPATH=apps/api/src python scripts/measure_recall_source_lookup.py build \
        --data-dir /tmp/recall-vault
    PYTHONPATH=apps/api/src python scripts/measure_recall_source_lookup.py measure \
        --data-dir /tmp/recall-vault --label branch

Point PYTHONPATH at another checkout's apps/api/src to time that version on the
same vault. ``measure`` copies the vault before it runs, so repeated runs and
different versions all start from identical bytes. ``build`` needs the current
tree; ``measure`` uses only the public MCP entry points and runs on v0.18.0 and
later.

Defaults match the report in the PR: 4,000 captured sources with one 2 KB chunk
each, 1,000 memories, about 19,000 events.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

USER_ID = "00000000-0000-0000-0000-000000000001"
TOPICS = 40
# The query asks about topic 0. About one source in forty belongs to it, so the
# search has far more matches than any call packs.
QUERY_TERMS = ("harbour", "ledger")
VOCABULARY_SIZE = 600


def _vocabulary() -> list[str]:
    rng = random.Random(7)
    consonants = "bcdfghjklmnprstvwz"
    vowels = "aeiou"
    words: set[str] = set()
    while len(words) < VOCABULARY_SIZE:
        length = rng.choice((2, 3, 3, 4))
        words.add("".join(rng.choice(consonants) + rng.choice(vowels) for _ in range(length)))
    return sorted(words)


def _topic_terms(topic: int, vocabulary: list[str]) -> tuple[str, str]:
    if topic == 0:
        return QUERY_TERMS
    return vocabulary[topic * 2], vocabulary[topic * 2 + 1]


def _chunk_text(rng: random.Random, topic: int, vocabulary: list[str], *, size: int = 2048) -> str:
    first, second = _topic_terms(topic, vocabulary)
    lines: list[str] = []
    length = 0
    while length < size:
        words = [rng.choice(vocabulary) for _ in range(9)]
        words[rng.randrange(9)] = first
        words[rng.randrange(9)] = second
        line = " ".join(words).capitalize() + "."
        lines.append(line)
        length += len(line) + 1
    return "\n".join(lines)[:size]


def build_vault(data_dir: Path, *, sources: int, memories: int, events: int) -> dict[str, int]:
    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    database = resolve_db_path(data_dir=str(data_dir), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    rng = random.Random(20260701)
    vocabulary = _vocabulary()
    source_rows: list[dict[str, object]] = []
    chunk_texts: dict[str, str] = {}
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(sources):
            topic = index % TOPICS
            text = _chunk_text(rng, topic, vocabulary)
            source = store.create_source(
                {
                    "source_type": "note",
                    "title": f"Captured note {index}",
                    "content_hash": f"hash-synthetic-{index}",
                    "captured_at": "2026-08-01T08:00:00Z",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"project_scope": ["harbour"], "raw_text": text},
                }
            )
            store.create_source_chunk(
                {
                    "source_id": source["id"],
                    "chunk_index": 0,
                    "text": text,
                    "token_count": len(text.split()),
                }
            )
            source_rows.append(source)
            chunk_texts[str(source["id"])] = text
        topic_zero = [row for index, row in enumerate(source_rows) if index % TOPICS == 0]
        made = 0
        stale = 0
        for index in range(memories):
            # The first 300 memories hang off topic-0 sources, the ones the
            # benchmark query packs. A tenth of those were later superseded,
            # so the correction label has real work to do.
            if index < 300:
                source = topic_zero[index % len(topic_zero)]
            else:
                source = source_rows[rng.randrange(len(source_rows))]
            source_text = chunk_texts[str(source["id"])]
            first, second = _topic_terms(index % TOPICS, vocabulary)
            body = f"Decision {index} about {first} and {second}: keep the {first} schedule fixed."
            is_stale = index < 300 and index % 10 == 0
            replacement_id: str | None = None
            if is_stale:
                replacement = store.create_memory(
                    {
                        "memory_key": f"synthetic.replacement.{index}",
                        "memory_type": "decision",
                        "title": f"Replacement {index}",
                        "canonical_text": f"Updated decision {index}: move the {first} schedule.",
                        "status": "active",
                        "domain": "project",
                        "sensitivity": "public",
                        "project_scope": ["harbour"],
                        "metadata_json": {"project_scope": ["harbour"]},
                        "value": {"text": f"Updated decision {index}"},
                    }
                )
                replacement_id = str(replacement["id"])
            memory = store.create_memory(
                {
                    "memory_key": f"synthetic.decision.{index}",
                    "memory_type": "decision",
                    "title": f"Decision {index}",
                    "canonical_text": body,
                    "status": "superseded" if is_stale else "active",
                    "superseded_by": replacement_id,
                    "domain": "project",
                    "sensitivity": "public",
                    "project_scope": ["harbour"],
                    "metadata_json": {"project_scope": ["harbour"]},
                    "value": {"text": body},
                }
            )
            store.create_provenance_link(
                {
                    "target_type": "memory",
                    "target_id": str(memory["id"]),
                    "source_id": source["id"],
                    "evidence_role": "quoted_from" if is_stale else "supports",
                    "quote": source_text.split("\n", 1)[0] if is_stale else None,
                    "confidence": 0.9,
                }
            )
            made += 1
            stale += int(is_stale)
        existing = connection.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()
        existing_count = int(existing["n"] if isinstance(existing, dict) else existing[0])
        for index in range(max(0, events - existing_count)):
            source = source_rows[index % len(source_rows)]
            store.append_event(
                {
                    "event_type": "source.touched",
                    "actor_type": "system",
                    "target_type": "source",
                    "target_id": str(source["id"]),
                    "payload_json": {"synthetic": True, "n": index},
                }
            )
        total_events = connection.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()
        total = int(total_events["n"] if isinstance(total_events, dict) else total_events[0])
    return {"sources": sources, "memories": made, "stale_memories": stale, "events": total}


class _StatementCounter:
    """Counts SQL statements by wrapping ``sqlite3.connect`` with a trace hook."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._connect = sqlite3.connect

    def install(self) -> None:
        counter = self

        def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
            connection = counter._connect(*args, **kwargs)
            connection.set_trace_callback(counter.statements.append)
            return connection

        sqlite3.connect = traced  # type: ignore[assignment]

    def restore(self) -> None:
        sqlite3.connect = self._connect  # type: ignore[assignment]

    def reset(self) -> None:
        self.statements.clear()

    def summary(self) -> dict[str, int]:
        selects = sum(1 for sql in self.statements if sql.lstrip().upper().startswith(("SELECT", "WITH")))
        lookups = sum(1 for sql in self.statements if "json_tree(m.metadata_json" in sql)
        return {"statements": len(self.statements), "selects": selects, "source_lookups": lookups}


def _count_store_calls() -> Counter[str]:
    """Count calls to the memory-by-source store methods and the ids each takes."""

    from alicebot_api.sqlite_store import SQLiteVNextStore

    calls: Counter[str] = Counter()
    for name in ("list_memories_referencing_source", "list_memories_referencing_sources"):
        original = getattr(SQLiteVNextStore, name, None)
        if original is None:
            continue

        def wrapper(self, *args, __original=original, __name=name, **kwargs):  # type: ignore[no-untyped-def]
            calls[__name] += 1
            if __name.endswith("sources"):
                ids = kwargs.get("source_ids", args[0] if args else ())
                calls[__name + ".ids"] += len(list(ids))
            return __original(self, *args, **kwargs)

        setattr(SQLiteVNextStore, name, wrapper)
    return calls


def _sources_in(payload: dict[str, object]) -> int:
    sources = payload.get("sources")
    if isinstance(sources, list):
        return len(sources)
    pack = payload.get("pack") or payload.get("context_pack")
    if isinstance(pack, dict) and isinstance(pack.get("sources"), list):
        return len(pack["sources"])
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("sources"), list):
            return len(value["sources"])
    return 0


def measure(data_dir: Path, *, runs: int, label: str) -> dict[str, object]:
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import resolve_db_path, sqlite_url_for_path

    for name in (
        "ALICE_AGENT_API_KEY",
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
    ):
        os.environ.pop(name, None)
    # alice_context_pack is part of the full MCP surface.
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    source_database = resolve_db_path(data_dir=str(data_dir), db=None)
    query = " ".join(QUERY_TERMS)
    cases: list[tuple[str, str, dict[str, object]]] = [
        ("recall (limit 8)", "alice_recall", {"query": query}),
        ("recall (limit 50)", "alice_recall", {"query": query, "limit": 50}),
        ("context pack", "alice_context_pack", {"query": query}),
    ]
    results: list[dict[str, object]] = []
    store_calls = _count_store_calls()
    counter = _StatementCounter()
    with tempfile.TemporaryDirectory() as scratch:
        working = Path(scratch) / "vault"
        working.mkdir()
        shutil.copytree(source_database.parent, working, dirs_exist_ok=True)
        database = resolve_db_path(data_dir=str(working), db=None)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
        counter.install()
        try:
            for case, tool, arguments in cases:
                call_mcp_tool(context, name=tool, arguments=dict(arguments))  # warm up
                samples: list[float] = []
                payload: dict[str, object] = {}
                for _ in range(runs):
                    counter.reset()
                    store_calls.clear()
                    started = time.perf_counter()
                    payload = call_mcp_tool(context, name=tool, arguments=dict(arguments))
                    samples.append((time.perf_counter() - started) * 1000.0)
                row: dict[str, object] = {
                    "case": case,
                    "median_ms": round(statistics.median(samples), 1),
                    "min_ms": round(min(samples), 1),
                    "max_ms": round(max(samples), 1),
                    "sources_packed": _sources_in(payload),
                    "store_calls": dict(store_calls),
                    **counter.summary(),
                }
                results.append(row)
        finally:
            counter.restore()
    return {"label": label, "runs": runs, "python": sys.version.split()[0], "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="seed the synthetic vault")
    build.add_argument("--data-dir", required=True)
    build.add_argument("--sources", type=int, default=4000)
    build.add_argument("--memories", type=int, default=1000)
    build.add_argument("--events", type=int, default=19000)
    run = sub.add_parser("measure", help="time the two tools on a vault")
    run.add_argument("--data-dir", required=True)
    run.add_argument("--runs", type=int, default=5)
    run.add_argument("--label", default="unlabelled")
    args = parser.parse_args(argv)
    if args.command == "build":
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        print(json.dumps(build_vault(data_dir, sources=args.sources, memories=args.memories, events=args.events)))
        return 0
    print(json.dumps(measure(Path(args.data_dir), runs=args.runs, label=args.label), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
