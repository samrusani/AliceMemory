"""Time the per-project reads on a synthetic SQLite vault (spec section 12).

What it answers. A brief in a project reads this project's notes and the notes
that belong to no project, instead of the newest notes of the whole vault. This
script measures what that costs against the unscoped read, at the vault sizes
the spec names (5,000 and 50,000 notes), and which fill shape fits the budget:

* ``unscoped``: the read of the previous release, no project fence.
* ``explicit``: the same brief under today's explicit one-project filter.
* ``single_scan``: the project view, one scan labels every row once into a
  materialized common table expression that is read twice, as a top-N per label
  (what ``alice-memory brief`` and the hook run).
* ``two_query``: the project view filled by two ordinary queries, project only
  and global only, each paying the full scan (the shape the spec priced first).

It also times the resolver, ``alice_resume`` in the project view, and recall and
the context pack with a project view tuple injected (those two tools are not
view-bearing yet, so the tuple is put in where the policy decision would put it).

Two steps, both offline, no network, no model, no embeddings, no host binary:

    PYTHONPATH=apps/api/src python scripts/measure_project_view.py build \\
        --data-dir /tmp/pv-vault --notes 5000
    PYTHONPATH=apps/api/src python scripts/measure_project_view.py measure \\
        --data-dir /tmp/pv-vault --label branch

``build`` makes a scratch git repository beside the vault (``<data-dir>-repo``,
hand-written ``.git``, no ``git`` process) and stamps three project ids on 15
percent of the notes, the rest carry no project (a tenth of those under the
free-form name ``acme``). Five percent of the global notes are in the sensitive
domains. ``measure`` copies the vault first, so every run starts from the same
bytes. Nothing here writes to a home folder or reads one.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

USER_ID = "00000000-0000-0000-0000-000000000001"
META_FILE = "measure_project_view.json"
REMOTE_URL = "https://example.com/acme/payments.git"
SENSITIVE_DOMAIN_LABELS = ("health", "family", "legal", "financial", "spiritual")
VOCABULARY_SIZE = 400
QUERY = "harbour ledger"


def _vocabulary() -> list[str]:
    rng = random.Random(11)
    consonants = "bcdfghjklmnprstvwz"
    vowels = "aeiou"
    words: set[str] = set()
    while len(words) < VOCABULARY_SIZE:
        length = rng.choice((2, 3, 3, 4))
        words.add("".join(rng.choice(consonants) + rng.choice(vowels) for _ in range(length)))
    return sorted(words)


def _make_repo(root: Path) -> None:
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "config").write_text(
        f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {REMOTE_URL}\n',
        encoding="utf-8",
    )


def build_vault(data_dir: Path, *, notes: int) -> dict[str, object]:
    from alicebot_api.onramp import bootstrap_database, resolve_db_path
    from alicebot_api.project_identity import detect_project
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    repo = data_dir.parent / (data_dir.name + "-repo")
    _make_repo(repo)
    detection = detect_project(argument=str(repo), env_project_dir=None, hook_cwd=None, process_cwd=None)
    if detection.context is None:
        raise SystemExit(f"the scratch repository did not resolve: {detection.outcome}")
    project_a = detection.context.ids[0]
    project_b = "prj_" + "b2" * 8
    project_c = "prj_" + "c3" * 8
    scoped_ids = (project_a, project_b, project_c)

    database = resolve_db_path(data_dir=str(data_dir), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    rng = random.Random(20261002)
    vocabulary = _vocabulary()

    def text(first: str, second: str, size: int = 400) -> str:
        words: list[str] = []
        length = 0
        while length < size:
            line = " ".join(rng.choice(vocabulary) for _ in range(9))
            words.append(line.capitalize() + ".")
            length += len(line) + 2
        words[0] = f"{first} {second}. " + words[0]
        return "\n".join(words)[:size]

    def scope_for(index: int) -> tuple[str, ...] | None:
        roll = rng.random()
        if roll < 0.15:
            return (scoped_ids[index % 3],)
        if roll < 0.15 + 0.85 * 0.10:
            return ("acme",)
        return None

    def domain_for(scope: tuple[str, ...] | None) -> tuple[str, str]:
        if scope is None and rng.random() < 0.05:
            return rng.choice(SENSITIVE_DOMAIN_LABELS), "private"
        return "project", "public"

    counts = {"memories": 0, "open_loops": 0, "sources": 0}
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(notes):
            scope = scope_for(index)
            domain, sensitivity = domain_for(scope)
            body = text("harbour" if index % 40 == 0 else rng.choice(vocabulary), "ledger" if index % 40 == 0 else rng.choice(vocabulary))
            payload: dict[str, object] = {
                "memory_key": f"synthetic.fact.{index}",
                "memory_type": "decision" if index % 9 == 0 else "semantic",
                "title": f"Fact {index}",
                "canonical_text": body,
                "status": "active",
                "confirmation_status": "confirmed",
                "domain": domain,
                "sensitivity": sensitivity,
                "value": {"text": body},
            }
            if scope is not None:
                payload["project_scope"] = list(scope)
                payload["metadata_json"] = {"project_scope": list(scope)}
                if len(scope) == 1:
                    payload["project_id"] = scope[0]
            store.create_memory(payload)
            counts["memories"] += 1
        for index in range(max(10, notes // 10)):
            scope = scope_for(index)
            domain, sensitivity = domain_for(scope)
            loop: dict[str, object] = {
                "title": f"Open loop {index}: {rng.choice(vocabulary)} {rng.choice(vocabulary)}",
                "domain": domain,
                "sensitivity": sensitivity,
            }
            if scope is not None:
                loop["metadata_json"] = {"project_scope": list(scope)}
                if len(scope) == 1:
                    loop["project_id"] = scope[0]
            store.create_open_loop(loop)
            counts["open_loops"] += 1
        for index in range(max(100, notes // 50)):
            scope = scope_for(index)
            domain, sensitivity = domain_for(scope)
            body = text("harbour" if index % 20 == 0 else rng.choice(vocabulary), "ledger" if index % 20 == 0 else rng.choice(vocabulary), 1200)
            metadata: dict[str, object] = {"raw_text": body}
            if scope is not None:
                metadata["project_scope"] = list(scope)
            source = store.create_source(
                {
                    "source_type": "note",
                    "title": f"Captured note {index}",
                    "content_hash": f"hash-synthetic-{index}",
                    "captured_at": "2026-09-01T08:00:00Z",
                    "domain": domain,
                    "sensitivity": sensitivity,
                    "metadata_json": metadata,
                }
            )
            store.create_source_chunk(
                {"source_id": source["id"], "chunk_index": 0, "text": body, "token_count": len(body.split())}
            )
            counts["sources"] += 1
        event_total = connection.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()
    total = int(event_total["n"] if isinstance(event_total, dict) else event_total[0])
    meta = {"notes": notes, "project_a": project_a, "repo": str(repo), **counts, "events": total}
    (database.parent / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _median(samples: Sequence[float]) -> float:
    return round(statistics.median(samples), 1)


def _time(fn: Callable[[], object], *, runs: int) -> tuple[float, float, float]:
    """Median, minimum and maximum CPU milliseconds of ``runs`` calls, after one warm-up.

    The clock is ``time.process_time``: this process's own CPU time. These reads are one
    thread of Python and SQLite with no waiting, so CPU time is what wall time is on an idle
    machine, and it is not stretched by other work on a shared machine (a build box, a laptop
    with other agents running), which made wall-clock medians from the same vault differ by
    five times between runs.
    """

    fn()  # warm up
    samples: list[float] = []
    for _ in range(runs):
        started = time.process_time()
        fn()
        samples.append((time.process_time() - started) * 1000.0)
    return _median(samples), round(min(samples), 1), round(max(samples), 1)


def _time_interleaved(
    cases: dict[str, Callable[[], object]], *, runs: int
) -> dict[str, tuple[float, float, float]]:
    """Time several cases round-robin, so a drift in the machine's load hits them all alike.

    Each case runs once to warm up, then ``runs`` rounds of every case in turn. A comparison of two cases
    timed one after the other can differ by a third between two runs of the same script on a busy machine,
    because the load changes between the two blocks. Interleaved, it does not.
    """

    for fn in cases.values():
        fn()
    samples: dict[str, list[float]] = {name: [] for name in cases}
    for _ in range(runs):
        for name, fn in cases.items():
            started = time.process_time()
            fn()
            samples[name].append((time.process_time() - started) * 1000.0)
    return {
        name: (_median(values), round(min(values), 1), round(max(values), 1)) for name, values in samples.items()
    }


def measure(data_dir: Path, *, runs: int, label: str) -> dict[str, object]:
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import resolve_db_path, sqlite_url_for_path
    from alicebot_api.project_identity import detect_project
    from alicebot_api.project_view import ProjectView
    from alicebot_api.session_briefing import (
        compile_local_session_brief,
        compile_session_brief,
        sensitive_global_exclusion,
    )
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_control import DEFAULT_AGENT_SENSITIVITY
    from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER

    for name in (
        "ALICE_AGENT_API_KEY",
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_PROJECT_SCOPING",
        "ALICE_PROJECT_DIR",
    ):
        os.environ.pop(name, None)
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    source_database = resolve_db_path(data_dir=str(data_dir), db=None)
    meta = json.loads((source_database.parent / META_FILE).read_text(encoding="utf-8"))
    repo = meta["repo"]
    detection = detect_project(argument=repo, env_project_dir=None, hook_cwd=None, process_cwd=None)
    assert detection.context is not None
    project = detection.context
    view = ProjectView.for_project(project)
    held_back = sensitive_global_exclusion(view)

    results: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as scratch:
        working = Path(scratch) / "vault"
        working.mkdir()
        shutil.copytree(source_database.parent, working, dirs_exist_ok=True)
        database = resolve_db_path(data_dir=str(working), db=None)

        def brief(project_view: ProjectView) -> str:
            return compile_local_session_brief(
                database,
                user_id=USER_ID,
                query=None,
                project_view=project_view,
                exclude_global_domains=sensitive_global_exclusion(project_view),
            )

        def explicit() -> str:
            with sqlite_user_connection(database, USER_ID) as connection:
                return compile_session_brief(
                    SQLiteVNextStore(connection, USER_ID),
                    effective_domains=(),
                    effective_sensitivity_allowed=DEFAULT_AGENT_SENSITIVITY,
                    effective_project_scope=(project.ids[0],),
                    project_view=ProjectView.unscoped(),
                    exclude_global_domains=frozenset(),
                    query=None,
                )

        # The two-query shape: the partition readers replaced by two ordinary reads.
        memory_partition = SQLiteVNextStore.list_memories_view_partitions
        loop_partition = SQLiteVNextStore.list_open_loops_view_partitions

        def two_query_memories(self, *, project_ids, exclude_global_domains, per_partition_limit, **kwargs):  # type: ignore[no-untyped-def]
            mine = self.list_memories(projects=tuple(project_ids), limit=per_partition_limit, **kwargs)
            theirs = self.list_memories(
                projects=(GLOBAL_PROJECT_MARKER,),
                limit=per_partition_limit,
                exclude_global_domains=tuple(exclude_global_domains),
                **kwargs,
            )
            return mine, theirs

        def two_query_loops(self, *, project_ids, exclude_global_domains, per_partition_limit, **kwargs):  # type: ignore[no-untyped-def]
            mine = self.list_open_loops(scope_projects=tuple(project_ids), limit=per_partition_limit, **kwargs)
            theirs = self.list_open_loops(
                scope_projects=(GLOBAL_PROJECT_MARKER,),
                limit=per_partition_limit,
                exclude_global_domains=tuple(exclude_global_domains),
                **kwargs,
            )
            return mine, theirs

        def two_query_brief() -> str:
            SQLiteVNextStore.list_memories_view_partitions = two_query_memories  # type: ignore[method-assign,assignment]
            SQLiteVNextStore.list_open_loops_view_partitions = two_query_loops  # type: ignore[method-assign,assignment]
            try:
                return brief(view)
            finally:
                SQLiteVNextStore.list_memories_view_partitions = memory_partition  # type: ignore[method-assign]
                SQLiteVNextStore.list_open_loops_view_partitions = loop_partition  # type: ignore[method-assign]

        brief_cases = _time_interleaved(
            {
                "unscoped": lambda: brief(ProjectView.unscoped()),
                "explicit": explicit,
                "single_scan": lambda: brief(view),
                "two_query": two_query_brief,
            },
            runs=runs,
        )
        results["brief_ms"] = {name: {"median": m, "min": lo, "max": hi} for name, (m, lo, hi) in brief_cases.items()}
        results["brief_added_ms"] = {
            "single_scan": round(brief_cases["single_scan"][0] - brief_cases["unscoped"][0], 1),
            "two_query": round(brief_cases["two_query"][0] - brief_cases["unscoped"][0], 1),
        }
        results["brief_chars"] = {
            "unscoped": len(brief(ProjectView.unscoped())),
            "single_scan": len(brief(view)),
        }

        resolver = _time(
            lambda: [
                detect_project(argument=repo, env_project_dir=None, hook_cwd=None, process_cwd=None)
                for _ in range(200)
            ],
            runs=runs,
        )
        results["resolver_ms_per_call"] = round(resolver[0] / 200, 3)

        context = MCPRuntimeContext(
            database_url=sqlite_url_for_path(database), user_id=USER_ID, project_dir=repo
        )
        def resume_with_scoping(value: str | None) -> Callable[[], object]:
            def call() -> object:
                if value is None:
                    os.environ.pop("ALICE_PROJECT_SCOPING", None)
                else:
                    os.environ["ALICE_PROJECT_SCOPING"] = value
                try:
                    return call_mcp_tool(context, name="alice_resume", arguments={})
                finally:
                    os.environ.pop("ALICE_PROJECT_SCOPING", None)

            return call

        resume_timed = _time_interleaved(
            {"unscoped": resume_with_scoping(None), "project_view": resume_with_scoping("on")}, runs=runs
        )
        resume_off, resume_on = resume_timed["unscoped"], resume_timed["project_view"]
        results["resume_ms"] = {
            "unscoped": {"median": resume_off[0], "min": resume_off[1], "max": resume_off[2]},
            "project_view": {"median": resume_on[0], "min": resume_on[1], "max": resume_on[2]},
        }

        # Recall and the pack are not view-bearing in this slice. Put the view's
        # tuple where the policy decision puts a project scope and time the same
        # calls, so the cost of the scoped path with the marker is on record.
        import alicebot_api.mcp.context as mcp_context
        import alicebot_api.mcp.retrieval as mcp_retrieval

        tuple_scope = (*project.ids, GLOBAL_PROJECT_MARKER)
        original_preflight = mcp_retrieval._mcp_agent_policy_preflight
        original_checked = mcp_context._policy_checked
        from dataclasses import replace

        def injected_preflight(*args, **kwargs):  # type: ignore[no-untyped-def]
            decision = original_preflight(*args, **kwargs)
            return replace(decision, effective_project_scope=tuple_scope)

        def injected_checked(*args, **kwargs):  # type: ignore[no-untyped-def]
            actor_type, actor_id, decision = original_checked(*args, **kwargs)
            return actor_type, actor_id, replace(decision, effective_project_scope=tuple_scope)

        retrieval_cases = (
            ("recall", "alice_recall", {"query": QUERY}),
            ("context_pack", "alice_context_pack", {"query": QUERY}),
        )
        retrieval_results: dict[str, object] = {}
        for case, tool, arguments in retrieval_cases:

            def plain(tool: str = tool, arguments: dict = arguments) -> object:
                return call_mcp_tool(context, name=tool, arguments=dict(arguments))

            def with_view(tool: str = tool, arguments: dict = arguments) -> object:
                mcp_retrieval._mcp_agent_policy_preflight = injected_preflight  # type: ignore[assignment]
                mcp_context._policy_checked = injected_checked  # type: ignore[assignment]
                try:
                    return call_mcp_tool(context, name=tool, arguments=dict(arguments))
                finally:
                    mcp_retrieval._mcp_agent_policy_preflight = original_preflight  # type: ignore[assignment]
                    mcp_context._policy_checked = original_checked  # type: ignore[assignment]

            timed = _time_interleaved({"unscoped": plain, "project_view": with_view}, runs=runs)
            base, scoped = timed["unscoped"], timed["project_view"]
            retrieval_results[case] = {
                "unscoped_median_ms": base[0],
                "project_view_median_ms": scoped[0],
                "added_ms": round(scoped[0] - base[0], 1),
            }
        results["retrieval_ms"] = retrieval_results
    return {
        "label": label,
        "runs": runs,
        "python": sys.version.split()[0],
        "sqlite": __import__("sqlite3").sqlite_version,
        "vault": {key: meta[key] for key in ("notes", "memories", "open_loops", "sources", "events")},
        "held_back_domains": sorted(held_back),
        **results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="seed the synthetic vault and its scratch repository")
    build.add_argument("--data-dir", required=True)
    build.add_argument("--notes", type=int, default=5000)
    run = sub.add_parser("measure", help="time the reads on a vault")
    run.add_argument("--data-dir", required=True)
    run.add_argument("--runs", type=int, default=5)
    run.add_argument("--label", default="unlabelled")
    args = parser.parse_args(argv)
    if args.command == "build":
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        print(json.dumps(build_vault(data_dir, notes=args.notes)))
        return 0
    print(json.dumps(measure(Path(args.data_dir), runs=args.runs, label=args.label), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
