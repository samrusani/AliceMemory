"""Builders, scenarios and the normalizer for the search goldens (spec 3.3, slice P0g).

``compute_goldens`` builds throwaway vaults from the invented folder in
``search_goldens_corpus``, runs every scenario through the same doors a person or an
agent uses (the MCP tool registry and the ``alice-memory`` CLI), and returns the
normalized results. ``fixtures_search_goldens`` holds what that returned when it ran
against main at the commit it names, and ``test_search_goldens`` compares the two.

Nothing here reads the environment, the home folder or a real vault. ``clean_environment``
removes every ``ALICE_`` and ``ALICEBOT_`` variable, which is what "every switch unset"
means, points ``HOME`` at a scratch folder, moves the working directory into a scratch folder
that is not a git checkout and leaves the MCP tool surface at its default.

Where the vault is built must not show in an output. The title and recency list of a source
search matches the words of a query, as substrings, against ``raw_path`` and ``metadata_json``,
and an import stores the absolute folder in both. A query with a one or two letter word (an
apostrophe splits "I'm" into ``m``) then lists a different number of sources depending on the
random name of the scratch folder: 2 builds in 150 listed 7 sources where the other 148 listed 4.
``pin_import_paths`` therefore rewrites that folder to a fixed one right after the imports of
the search vault, and a test builds the whole set again under a folder named with the words of
every query.

Normalization. A run mints ids and takes timestamps from the clock, so those are the only
values that change between two runs of one fixture:

* an id that names a row the vault holds (a source, a memory or an entity) becomes a label
  that names the row (``<source:runbook#1>``, ``<memory:fact.release.indigo#1>``), so a golden
  still shows which row came back and that two outputs name the same row;
* any other UUID (a pack id, a trace id, a confirmation id) becomes ``<id>``;
* an ISO timestamp becomes ``<ts>``.

To record the goldens again after a change that is meant to move an output, run this from the
repository root, with the commit of main the code is and the release tag below it::

    PYTHONPATH=apps/api/src python -m tests.unit.search_goldens_support --commit <sha> --tag v0.20.0 --write

The diff of ``fixtures_search_goldens.py`` is then the proof of what moved. Without ``--write`` the
text is printed and nothing is changed.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.onramp import main as onramp_main
from alicebot_api.recall_framing import serialize_mcp_tool_result
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from tests.unit import search_goldens_corpus as corpus

USER_ID = "00000000-0000-0000-0000-000000000001"

#: The environment prefixes that configure the product. Every variable that starts with
#: one of these is removed while the goldens run.
ENVIRONMENT_PREFIXES = ("ALICE_", "ALICEBOT_")

#: Switches this release adds. Listed so a test can say they were unset, whatever the
#: prefix rule above does.
SEARCH_RELEASE_SWITCHES = ("ALICE_SEARCH_QUALITY", "ALICE_MCP_COMMIT_RESULT")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


@contextlib.contextmanager
def clean_environment(work: Path) -> Iterator[None]:
    """No product variable set, ``HOME`` and the working directory in scratch, the default MCP surface.

    ``work`` is the scratch folder the run builds in. ``HOME`` becomes ``work/home`` and the working
    directory ``work/cwd``, a folder that is not inside a repository, so nothing resolves a project from
    where the test was started. The previous environment and directory are restored on exit. Nothing here
    is a pytest fixture, so the recording command and the test run under the same rules.
    """

    saved = dict(os.environ)
    saved_cwd = Path.cwd()
    home = work / "home"
    cwd = work / "cwd"
    try:
        for name in list(os.environ):
            if name.startswith(ENVIRONMENT_PREFIXES):
                del os.environ[name]
        home.mkdir(parents=True, exist_ok=True)
        cwd.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home)
        os.chdir(cwd)
        yield
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def full_surface() -> Iterator[None]:
    """The full tool surface, for the two review tools the corrected-source fixture needs."""

    previous = os.environ.get(MCP_FULL_TOOLS_ENV)
    os.environ[MCP_FULL_TOOLS_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(MCP_FULL_TOOLS_ENV, None)
        else:
            os.environ[MCP_FULL_TOOLS_ENV] = previous


# ----------------------------------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------------------------------


def read_id_labels(db_path: Path) -> dict[str, str]:
    """Map the id of every source, memory and entity in the vault to a label that names it.

    A source is named by its title and a memory by its title (its key when it has none). Rows
    that share a name are numbered in the order they were written, so the label of the first
    runbook stays ``<source:runbook#1>`` when an edited copy arrives as ``#2``. Titles come from
    the fixture, not from the run, so a label is the same on every run.
    """

    labels: dict[str, str] = {}
    connection = sqlite3.connect(str(db_path))
    try:
        for kind, table, column, order in (
            ("source", "sources", "title", "rowid"),
            ("memory", "memories", "COALESCE(title, memory_key)", "rowid"),
            ("entity", "vnext_entities", "name", "rowid"),
        ):
            counts: dict[str, int] = {}
            for row_id, name in connection.execute(f"SELECT id, {column} FROM {table} ORDER BY {order}"):
                plain = str(name)
                counts[plain] = counts.get(plain, 0) + 1
                suffix = f"#{counts[plain]}" if kind != "entity" else ""
                labels[str(row_id)] = f"<{kind}:{plain}{suffix}>"
    finally:
        connection.close()
    return labels


def normalize_text(text: str, labels: Mapping[str, str]) -> str:
    """Replace ids and timestamps in one string, in a single pass so a label is never rescanned."""

    def _id(match: re.Match[str]) -> str:
        return labels.get(match.group(0), "<id>")

    return _STAMP.sub("<ts>", _UUID.sub(_id, text))


def normalize_value(value: object, labels: Mapping[str, str]) -> object:
    """Normalize every string leaf and sort every mapping, so equal outputs compare equal."""

    if isinstance(value, dict):
        return {key: normalize_value(child, labels) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_value(child, labels) for child in value]
    if isinstance(value, str):
        return normalize_text(value, labels)
    return value


# ----------------------------------------------------------------------------------------------
# Building vaults
# ----------------------------------------------------------------------------------------------


class Vault:
    """One throwaway vault: its data folder, database file and MCP context."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.db_path = resolve_db_path(data_dir=str(data_dir), db=None)
        bootstrap_database(self.db_path, user_id=USER_ID, user_email="local@alice")
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.db_path), user_id=USER_ID)

    def labels(self) -> dict[str, str]:
        return read_id_labels(self.db_path)

    def call(self, name: str, arguments: Mapping[str, object], *, full: bool = False) -> dict[str, object]:
        """One MCP tool call, on the default surface unless the tool lives only on the full one."""

        if full:
            with full_surface():
                return call_mcp_tool(self.context, name=name, arguments=dict(arguments))
        return call_mcp_tool(self.context, name=name, arguments=dict(arguments))

    def cli(self, *argv: str) -> tuple[int, str, str]:
        """Run ``alice-memory`` in this process against this vault; return code, stdout and stderr."""

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = onramp_main([*argv, "--data-dir", str(self.data_dir)])
        return code, out.getvalue(), err.getvalue()

    def import_folder(self, folder: Path, *, domain: str, sensitivity: str) -> tuple[int, str, str]:
        return self.cli("import-markdown", "--from", str(folder), "--domain", domain, "--sensitivity", sensitivity)

    def source_id_by_title(self, title: str, *, nth: int = 1) -> str:
        connection = sqlite3.connect(str(self.db_path))
        try:
            rows = connection.execute("SELECT id FROM sources WHERE title = ? ORDER BY rowid", (title,)).fetchall()
        finally:
            connection.close()
        return str(rows[nth - 1][0])


#: The folder every import of the search vault is rewritten to. It holds no word a scenario query uses
#: (``test_search_goldens`` checks that), because the source search matches query words against it.
PINNED_IMPORT_ROOT = "/qzvk/qzvk"


def pin_import_paths(vault: Vault, real_root: Path) -> int:
    """Rewrite the absolute folder an import stored to ``PINNED_IMPORT_ROOT``; return the rows changed.

    The importer stores the resolved absolute path in ``sources.raw_path`` and in ``metadata_json``
    (``folder``), and the title and recency list of a source search matches query words against both. Left
    alone, the path of the scratch folder decides how many sources a query with a short word lists (see the
    module docstring). Only the folder prefix is replaced, in SQL, so the rest of each ``metadata_json``
    string is byte for byte what the importer wrote. Raises when a row still holds the real folder.
    """

    real = str(real_root.resolve())
    escaped = json.dumps(real)[1:-1]
    connection = sqlite3.connect(str(vault.db_path))
    try:
        with connection:
            changed = connection.execute(
                "UPDATE sources SET raw_path = REPLACE(raw_path, ?, ?), metadata_json = REPLACE(metadata_json, ?, ?)"
                " WHERE instr(COALESCE(raw_path, ''), ?) > 0 OR instr(metadata_json, ?) > 0",
                (real, PINNED_IMPORT_ROOT, escaped, PINNED_IMPORT_ROOT, real, escaped),
            ).rowcount
        left = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE instr(COALESCE(raw_path, ''), ?) > 0 OR instr(metadata_json, ?) > 0",
            (real, escaped),
        ).fetchone()[0]
    finally:
        connection.close()
    if left:
        raise AssertionError(f"{left} source rows still name the folder the vault was built in")
    return int(changed)


def _memory_payload(
    key: str, text: str, *, memory_type: str, domain: str = "project", sensitivity: str = "internal"
) -> dict[str, object]:
    return {
        "memory_key": key,
        "memory_type": memory_type,
        "title": key,
        "canonical_text": text,
        "status": "active",
        "confirmation_status": "confirmed",
        "domain": domain,
        "sensitivity": sensitivity,
        "value": {"text": text},
    }


def build_search_vault(work: Path) -> Vault:
    """The vault the recall, pack and brief goldens read.

    Five documents of the main folder (project, internal), one confidential vendor note, one
    private personal garden note, four stored memories (three linked to a source, one not) and
    one source whose derived memory was corrected after capture. The order is fixed, and the
    recency list breaks ties by capture time, so the outputs are the same on every run.
    """

    docs = work / "docs"
    corpus.write_folder(docs, corpus.MAIN_FOLDER_FILES)
    corpus.write_folder(docs, (corpus.VENDOR_FILE, corpus.GARDEN_FILE))
    vault = Vault(work / "search-vault")
    for folder, domain, sensitivity in (
        (docs / "harbor-lantern", "project", "internal"),
        (docs / "vendor", "project", "confidential"),
        (docs / "home", "personal", "private"),
    ):
        code, _out, err = vault.import_folder(folder, domain=domain, sensitivity=sensitivity)
        assert code == 0, err
    assert pin_import_paths(vault, docs) == 7

    runbook = vault.source_id_by_title("runbook")
    ledger = vault.source_id_by_title("ledger-design")
    with sqlite_user_connection(vault.db_path, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for key, text, kind, source_id in (
            ("fact.release.indigo", "The indigo checklist is run by whoever tags the release.", "decision", runbook),
            (
                "fact.rollback.signoff",
                "A second engineer signs off before a yanked wheel is republished.",
                "decision",
                runbook,
            ),
            ("fact.ledger.corrections", "Ledger entries are corrected by appending a new entry.", "semantic", ledger),
            ("fact.support.hours", "Support hours are covered by the pager owner outside weekdays.", "semantic", runbook),
            ("fact.style.reviews", "We prefer small reviews with one concern each.", "semantic", None),
        ):
            memory = store.create_memory(_memory_payload(key, text, memory_type=kind))
            if source_id is not None:
                store.create_provenance_link(
                    {
                        "target_type": "memory",
                        "target_id": str(memory["id"]),
                        "source_id": source_id,
                        "evidence_role": "supports",
                        "confidence": 0.9,
                    }
                )

    # A captured note whose derived memory is later superseded: the source excerpt is labelled
    # stale and names the current memory. Capture, review and correct are the tools an agent has.
    old = "The pager handover happens on Monday at nine."
    new = "The pager handover happens on Tuesday at nine."
    captured = vault.call(
        "alice_capture",
        {"raw_text": f"Fact: {old}", "title": "Pager handover note", "domain": "project", "sensitivity": "internal"},
        full=True,
    )
    assert captured["status"] == "imported", captured
    review = vault.call("alice_memory_review", {"status": "all", "limit": 20}, full=True)
    items = review["items"]
    assert isinstance(items, list)
    candidate = next(str(item["id"]) for item in items if old in json.dumps(item, default=str))
    corrected = vault.call(
        "alice_memory_correct",
        {
            "action": "supersede-existing",
            "review_item_id": candidate,
            "replacement_title": "Pager handover",
            "replacement_body": {"text": new},
            "reason": "the day moved",
        },
        full=True,
    )
    assert corrected.get("replacement_object"), corrected
    return vault


# ----------------------------------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------------------------------

#: name -> arguments of one ``alice_recall`` call, in the order they run.
RECALL_SCENARIOS: dict[str, dict[str, object]] = {
    "sections_of_one_document": {
        "query": "What does the Harbor Lantern runbook say about the release gate, tagging and rolling back?"
    },
    "keywords_one_strict_hit": {"query": "annotated tag merge commit"},
    "contraction_and_version": {"query": "I'm not sure why it doesn't print secrets in v0.20.0"},
    "two_terms": {"query": "pager rota"},
    "same_text_in_two_sources": {"query": "support hours weekdays nine to five"},
    "ledger_sections": {"query": "ledger retry append only compaction"},
    "memory_names_a_source": {"query": "indigo checklist tags the release"},
    "memory_without_a_source": {"query": "small reviews one concern each"},
    "hop_follows_the_first_source": {"query": "retry backoff jitter queue alert"},
    "entity_name": {"query": "Orla Vance"},
    "limit_three": {"query": "release gate tag rolling back pager support hours", "limit": 3},
    "sources_off": {"query": "release gate tagging", "include_sources": False},
    "domain_project": {"query": "tomato beds frost covers", "domains": ["project"]},
    "domain_unfiltered": {"query": "tomato beds frost covers"},
    "confidential_hidden": {"query": "vendor shortlist Ember Quay supplier"},
    "confidential_allowed": {
        "query": "vendor shortlist Ember Quay supplier",
        "sensitivity_allowed": ["confidential"],
    },
    "corrected_source": {"query": "pager handover Monday at nine"},
    "debug_trace": {"query": "release gate tagging", "debug": True},
    "minimal_depth": {"query": "pager rota", "context_depth": "minimal"},
}

#: The one recall whose wire text (the string an MCP host hands the model) is pinned.
RECALL_WIRE_SCENARIO = "sections_of_one_document"

PACK_SCENARIOS: dict[str, dict[str, object]] = {
    "question": {"query": "How do we tag a release and who rolls it back?"},
    "keywords": {"query": "ledger retry compaction"},
    "memory_and_source": {"query": "indigo checklist release tag"},
    "small_budget": {
        "query": "release gate tagging rolling back pager ledger retry support hours",
        "max_tokens": 500,
    },
    "sources_first": {
        "query": "release gate tagging rolling back pager",
        "budget_strategy": "sources_first",
    },
}

BRIEF_SCENARIOS: dict[str, tuple[str, ...]] = {
    "no_query": (),
    "release_query": ("--query", "release gate tagging"),
    "ledger_query": ("--query", "ledger retry append"),
}

#: name -> arguments of one ``alice_memory_commit`` call. ``confirm`` names the scenario whose
#: confirmation id the call finishes.
_TRUSTED_AGENT = {"agent_id": "builder-1", "agent_type": "coding_agent", "permission_profile": "trusted_local_agent"}
COMMIT_SCENARIOS: dict[str, dict[str, object]] = {
    "committed": {
        "title": "Release gate",
        "canonical_text": "The release gate must be green before any tag is pushed.",
        "memory_type": "decision",
        "domain": "project",
        "sensitivity": "internal",
        "idempotency_key": "gate-1",
    },
    "replay": {
        "title": "Release gate",
        "canonical_text": "The release gate must be green before any tag is pushed.",
        "memory_type": "decision",
        "domain": "project",
        "sensitivity": "internal",
        "idempotency_key": "gate-1",
    },
    "confirmation_required": {
        "title": "Friday tags",
        "canonical_text": "The tag may be pushed on Fridays.",
        "memory_type": "decision",
        "domain": "project",
        "sensitivity": "internal",
        "confidence": 0.7,
    },
    "finish_confirmation": {"confirm": "confirmation_required", "confirmation_action": "confirm"},
    "review_required": {
        "title": "Pager rota",
        "canonical_text": "Perhaps the pager rota changes monthly.",
        "domain": "project",
        "sensitivity": "internal",
        "confidence": 0.3,
    },
    "committed_with_identity": {
        "title": "Ledger retries",
        "canonical_text": "Ledger writers retry a failed append three times.",
        "memory_type": "decision",
        "domain": "project",
        "sensitivity": "internal",
        **_TRUSTED_AGENT,
    },
    "rejected_above_ceiling": {
        "title": "Vendor",
        "canonical_text": "Ember Quay is the preferred supplier.",
        "domain": "project",
        "sensitivity": "confidential",
        **_TRUSTED_AGENT,
    },
    "rejected_read_only": {
        "title": "Read only",
        "canonical_text": "A read only agent cannot write.",
        "domain": "project",
        "sensitivity": "internal",
        "agent_id": "reader-1",
        "agent_type": "coding_agent",
        "permission_profile": "read_only_agent",
    },
}

#: The wire text of this commit result is pinned beside its normalized form.
COMMIT_WIRE_SCENARIO = "committed"


def scenario_queries() -> list[str]:
    """The query text of every recall, pack and brief scenario, for the test that guards ``PINNED_IMPORT_ROOT``."""

    queries = [str(arguments["query"]) for arguments in (*RECALL_SCENARIOS.values(), *PACK_SCENARIOS.values())]
    for flags in BRIEF_SCENARIOS.values():
        if "--query" in flags:
            queries.append(flags[flags.index("--query") + 1])
    return queries


def _recall(vault: Vault) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for name, arguments in RECALL_SCENARIOS.items():
        result = vault.call("alice_recall", arguments)
        entry: dict[str, object] = {
            "arguments": copy.deepcopy(arguments),
            "result": normalize_value(result, vault.labels()),
        }
        if name == RECALL_WIRE_SCENARIO:
            entry["wire_text"] = normalize_text(serialize_mcp_tool_result(result), vault.labels())
        scenarios[name] = entry
    return scenarios


def _packs(vault: Vault) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for name, arguments in PACK_SCENARIOS.items():
        result = vault.call("alice_context_pack", arguments, full=True)
        scenarios[name] = {
            "arguments": copy.deepcopy(arguments),
            "result": normalize_value(result, vault.labels()),
        }
    return scenarios


def _briefs(vault: Vault) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for name, flags in BRIEF_SCENARIOS.items():
        code, out, err = vault.cli("brief", *flags)
        assert code == 0 and err == "", (name, code, err)
        scenarios[name] = {"arguments": list(flags), "printed": normalize_text(out, vault.labels())}
    return scenarios


def _vault_summary(vault: Vault) -> dict[str, object]:
    """What an import left behind: counts, the live sources, entity mentions and event types."""

    connection = sqlite3.connect(str(vault.db_path))
    try:

        def scalar(sql: str) -> int:
            return int(connection.execute(sql).fetchone()[0])

        sources = [
            f"{title}#{index} {domain}/{sensitivity} chunks={chunks} {'live' if live else 'deleted'}"
            for index, (title, domain, sensitivity, chunks, live) in _numbered(
                connection.execute(
                    "SELECT s.title, s.domain, s.sensitivity,"
                    " (SELECT COUNT(*) FROM source_chunks c WHERE c.source_id = s.id),"
                    " s.deleted_at IS NULL FROM sources s ORDER BY s.rowid"
                ).fetchall()
            )
        ]
        entities = {
            str(name): int(count)
            for name, count in connection.execute(
                "SELECT name, mention_count FROM vnext_entities WHERE deleted_at IS NULL ORDER BY name"
            )
        }
        events: dict[str, int] = {}
        for event_type, count in connection.execute(
            "SELECT event_type, COUNT(*) FROM event_log GROUP BY event_type ORDER BY event_type"
        ):
            events[str(event_type)] = int(count)
        return {
            "edge_count": scalar("SELECT COUNT(*) FROM graph_edges"),
            "chunk_count": scalar("SELECT COUNT(*) FROM source_chunks"),
            "entity_mentions": entities,
            "event_counts": events,
            "sources": sources,
        }
    finally:
        connection.close()


def _numbered(rows: list[tuple[object, ...]]) -> Iterator[tuple[int, tuple[object, ...]]]:
    """Number each row among the rows that share its title, in order, as the id labels do."""

    seen: dict[object, int] = {}
    for row in rows:
        seen[row[0]] = seen.get(row[0], 0) + 1
        yield seen[row[0]], row


def _imports(work: Path) -> dict[str, object]:
    """Receipts of ``alice-memory import-markdown`` through a sequence of imports, with the rows after each."""

    docs = work / "import-docs"
    corpus.write_folder(docs, corpus.MAIN_FOLDER_FILES)
    folder = docs / "harbor-lantern"
    vault = Vault(work / "import-vault")
    steps: dict[str, object] = {}

    def run(name: str, from_path: Path, *, domain: str, sensitivity: str) -> None:
        code, out, err = vault.import_folder(from_path, domain=domain, sensitivity=sensitivity)
        labels = vault.labels()
        receipt = json.loads(out)
        # The printed form is part of the contract: one line, sorted keys, ASCII.
        assert out == json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n", out
        steps[name] = {
            "arguments": ["import-markdown", "--from", from_path.name, "--domain", domain, "--sensitivity", sensitivity],
            "exit_code": code,
            "stderr": err,
            "receipt": normalize_value(receipt, labels),
            "vault_after": normalize_value(_vault_summary(vault), labels),
        }

    run("first_import", folder, domain="project", sensitivity="internal")
    run("unchanged_reimport", folder, domain="project", sensitivity="internal")
    (folder / "runbook.md").write_text(corpus.RUNBOOK_EDITED, encoding="utf-8")
    run("edited_reimport", folder, domain="project", sensitivity="internal")
    (folder / "faq-copy.md").write_text(corpus.FAQ, encoding="utf-8")
    run("copy_of_another_file", folder, domain="project", sensitivity="internal")
    run("relabelled_reimport", folder, domain="project", sensitivity="private")
    run("single_file", folder / "handbook.md", domain="personal", sensitivity="private")
    return steps


def _commits(work: Path) -> dict[str, object]:
    vault = Vault(work / "commit-vault")
    results: dict[str, dict[str, object]] = {}
    scenarios: dict[str, object] = {}
    for name, arguments in COMMIT_SCENARIOS.items():
        call_arguments = dict(arguments)
        confirm = call_arguments.pop("confirm", None)
        if confirm is not None:
            call_arguments["confirmation_id"] = results[str(confirm)]["confirmation_id"]
        result = vault.call("alice_memory_commit", call_arguments)
        results[name] = result
        entry: dict[str, object] = {
            "arguments": copy.deepcopy(arguments),
            "result": normalize_value(result, vault.labels()),
        }
        if name == COMMIT_WIRE_SCENARIO:
            entry["wire_text"] = normalize_text(serialize_mcp_tool_result(result), vault.labels())
        scenarios[name] = entry
    return scenarios


def compute_goldens(work: Path) -> dict[str, dict[str, object]]:
    """Run every scenario under a clean environment and return the normalized outputs.

    ``work`` is an empty scratch folder. The result has one key per surface: ``recall``,
    ``context_pack``, ``brief``, ``import_receipt`` and ``commit_result``. Each maps a scenario
    name to the arguments it ran with and what came back.
    """

    with clean_environment(work):
        search_vault = build_search_vault(work)
        return {
            "recall": _recall(search_vault),
            "context_pack": _packs(search_vault),
            "brief": _briefs(search_vault),
            "import_receipt": _imports(work),
            "commit_result": _commits(work),
        }


# ----------------------------------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).with_name("fixtures_search_goldens.py")

FIXTURE_HEADER = (
    '"""What main printed for the search-quality outputs, before any search change, pinned.\n'
    "\n"
    "Recorded by ``python -m tests.unit.search_goldens_support --commit <sha> --tag <tag> --write`` from\n"
    "the invented folder in ``search_goldens_corpus``, with no ``ALICE_`` variable set (so every\n"
    "search-release switch unset), on the commit and tag named in each surface's ``computed_from``. An id\n"
    "that names a row became a label, another id became ``<id>`` and a timestamp ``<ts>``. The file is\n"
    "never edited by hand: a change that moves an output records it again, and the diff of this file is the\n"
    "proof of what moved. ``test_search_goldens`` compares today's code against it in the ordinary test job.\n"
    '"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "import json\n"
    "\n"
    "GOLDENS: dict[str, dict[str, object]] = json.loads(\n"
    "    r'''\n"
)
FIXTURE_FOOTER = "\n'''\n)\n"


def render_fixture(goldens: Mapping[str, object]) -> str:
    body = json.dumps(goldens, indent=1, sort_keys=True, ensure_ascii=True)
    return FIXTURE_HEADER + body + FIXTURE_FOOTER


def record(commit: str, tag: str) -> str:
    """Compute every golden in a scratch folder and return the text of the fixture module.

    ``commit`` and ``tag`` are what the code under test is: the commit of main the outputs came from
    and the latest release tag below it. They are written into every surface, never guessed.
    """

    with tempfile.TemporaryDirectory(prefix="search-goldens-") as scratch:
        computed = compute_goldens(Path(scratch))
    provenance = {"commit": commit, "tag": tag}
    return render_fixture(
        {surface: {"computed_from": dict(provenance), "scenarios": scenarios} for surface, scenarios in computed.items()}
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Record the search goldens from the code in this working tree.")
    parser.add_argument("--commit", required=True, help="the 40 character commit of main the code is")
    parser.add_argument("--tag", required=True, help="the latest release tag below that commit, such as v0.20.0")
    parser.add_argument("--write", action="store_true", help="overwrite fixtures_search_goldens.py")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit) or not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        parser.error("--commit must be 40 lowercase hex characters and --tag must look like v0.20.0")
    text = record(args.commit, args.tag)
    if args.write:
        FIXTURE_PATH.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
