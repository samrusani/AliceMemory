"""A database written by v0.20.0 holds reports whose citations are in every shape, and the upgrade reads them all.

The consolidation report of v0.20.0 copies, as strings, the ``source_refs`` of the memories it clusters, and a client wrote
those strings. A report that cites a confidential memory as JSON text (``{"memory_id": "<id>", "quote": "..."}``) was stored
``internal``; the derived-label migration read only the plain ``memory:<id>`` string, so after the upgrade a project-bound
``project_scoped_agent`` key was answered HTTP 200 with the quote and the memory id, and the same after the memory was
redacted. These tests run the real migrations over a v0.20.0-shaped database and ask every door:

* the upgrade 0094 -> 0095 -> 0096 -> 0097 raises the report, and a key below the memory gets a 404 where the owner and an
  unbound admin key read it;
* revision 0097 repairs a database that already ran 0096 with the narrower reading;
* an upgrade that fails part way, and a database restored from a backup, end with no derived row below its inputs;
* a row that was never repaired (a backup restored at the head) is read at the label of its inputs, and ``labels repair``
  raises it;
* after the cited memory is redacted the report is readable by the owner and an unbound admin key only, until a report is
  made without the citation;
* every id the saved-quote reader finds in a derived row, the incidental ones too (under ``origin``, in a sentence, in
  ``mem:<id>``, in JSON text that does not decode), is an input when it names a stored memory or source and is ignored when
  it names none, so a list repr, a Markdown link, a trailing comma or an id that names nothing never holds a row back;
* a word that marks itself as a citation and holds an id the scan cannot take apart leaves the row unverified when that id
  names a stored row, and only then;
* every door that returns a report, a memory or a count of them answers a restricted key without the quote.

Mutations (``scripts/derived_label_mutations.json``): drop the soft candidates of ``_derived_dependency_record``; let the
settlement take a candidate that names no stored row as missing; let ``scan_citations`` skip the reference positions; drop
``unreadable`` from the settlement; drop the 0097 revision.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from alembic import command
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api import vnext_derived_labels
from alicebot_api import vnext_label_repair as repair
from alicebot_api.cli import labels
from alicebot_api.config import Settings
from alicebot_api.db import close_connection_pools, user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_repair import classify_stored_labels, load_postgres_label_tables
from alicebot_api.vnext_source_fence import cited_memory_refs, cited_source_ids
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.integration.derived_labels_postgres_support import LabelHarness
from tests.integration.redaction_containment_support import keyless_owner, leaks, owner_artifact, sweep
from tests.integration.conftest import _create_role_separated_database, _drop_database
from tests.integration.test_derived_labels_migration_postgres import assert_roles_and_force, database_urls  # noqa: F401
from tests.unit.legacy_citation_support import (
    INCIDENTAL_NAMES,
    SPELLINGS,
    candidate_memory,
    corpus_entries,
    memory_shapes,
    odd_hyphens,
    open_loop,
    report,
    source_shapes,
)

SENTINEL = "ZQXCITED" + uuid4().hex[:12]
RAISED = ("health", "confidential")
# What each key reads of a report that cites a confidential health memory, and of a report that is unverified. The unbound
# admin key and the owner read both. A project-bound admin key reads the first (its ceiling is every sensitivity and the
# report is inside its project) and is refused the second, as an unverified row is read by no bound key.
PROFILES = {
    "admin": ("admin_agent", False),
    "admin_bound": ("admin_agent", True),
    "trusted": ("trusted_local_agent", False),
    "trusted_bound": ("trusted_local_agent", True),
    "read_only": ("read_only_agent", False),
    "memory_proposal": ("memory_proposal_agent", False),
    "alpha_only": ("project_scoped_agent", True),
    "project_scoped": ("project_scoped_agent", False),
}
READS_RAISED = {"admin": True, "admin_bound": True}
READS_UNVERIFIED = {"admin": True}


def _memory_entry(shape: str, spelling: str, memory_id: str) -> object:
    return memory_shapes(SENTINEL)[shape](SPELLINGS[spelling](memory_id))


def _source_entry(shape: str, spelling: str, source_id: str) -> object:
    return source_shapes(SENTINEL)[shape](SPELLINGS[spelling](source_id))


class Scene:
    """A v0.20.0-shaped database: rows stored at the label they were made with, citations as the writers sent them."""

    def __init__(self, urls: dict[str, str]) -> None:
        self.urls = urls
        self.user = uuid4()
        self.artifacts: dict[str, str] = {}
        self.memories: dict[str, str] = {}
        self.loops: dict[str, str] = {}
        self.unreadable: set[str] = set()
        self.outcome: dict[str, str] = {}
        self.cites_memory: set[str] = set()
        with without_insert_floor(), user_connection(urls["app"], self.user) as conn:
            ContinuityStore(conn).create_user(self.user, f"legacy-{self.user}@example.invalid", "Synthetic")
            store = _store(conn)
            self.project = str(
                store.create_project({"slug": "alpha", "name": "Alpha", "domain": "project", "sensitivity": "public"})["id"]
            )
            self.source = str(
                store.create_source(
                    {"source_type": "note", "title": "Clinic note", "content_hash": "sha256:" + uuid4().hex,
                     "domain": "health", "sensitivity": "confidential",
                     "metadata_json": {"project_scope": [self.project], "raw_text": SENTINEL}}
                )["id"]
            )
            self.public_source = str(
                store.create_source(
                    {"source_type": "note", "title": "Public note", "content_hash": "sha256:" + uuid4().hex,
                     "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [self.project]}}
                )["id"]
            )
            self.memory = str(
                store.create_memory(
                    {"memory_key": "clinic.code", "memory_type": "semantic", "canonical_text": SENTINEL, "title": "Kiln code",
                     "status": "active", "domain": "health", "sensitivity": "confidential", "value": {"text": SENTINEL},
                     "project_id": self.project, "metadata_json": {"project_scope": [self.project]}}
                )["id"]
            )

    def expected(self, entries: list[object], *, unreadable: bool = False) -> str:
        """What the upgrade does to a row that holds ``entries`` in a reference position, by the saved-quote reader and not by
        the closure.

        ``raised``: the reader finds, named or incidental, the id of the confidential memory or source (it is a stored row).
        ``unverified``: the caller says a word of the entry marks itself as a citation and holds that id in a spelling the
        reader cannot take apart. ``unchanged``: the reader finds neither, whatever else the entries hold.
        """
        found = set(cited_source_ids(entries).every) | set(cited_memory_refs(entries).every)
        if unreadable:
            return "unverified"
        return "raised" if found & {self.memory, self.source} else "unchanged"

    def add_report(self, name: str, entries: list[object], *, position: str = "source_refs", unreadable: bool = False) -> str:
        row = report(entries, position=position)
        row["metadata_json"]["project_scope"] = [self.project]  # type: ignore[index]
        with without_insert_floor(), user_connection(self.urls["app"], self.user) as conn:
            created = _store(conn).create_artifact(row)
        self.artifacts[name] = str(created["id"])
        self.outcome[name] = self.expected(entries, unreadable=unreadable)
        if unreadable or self.memory in set(cited_source_ids(entries).every) | set(cited_memory_refs(entries).every):
            self.cites_memory.add(name)
        if self.outcome[name] == "unverified":
            self.unreadable.add(name)
        return str(created["id"])

    def add_candidate(self, name: str, entries: list[object], *, position: str = "source_refs") -> str:
        row = candidate_memory(entries, position=position)
        row["memory_key"] = f"legacy-candidate-{name}"
        row["metadata_json"]["project_scope"] = [self.project]  # type: ignore[index]
        with without_insert_floor(), user_connection(self.urls["app"], self.user) as conn:
            created = _store(conn).create_memory(row)
        self.memories[name] = str(created["id"])
        return str(created["id"])

    def add_loop(self, name: str, entries: list[object]) -> str:
        row = open_loop(entries, source_id=self.public_source)
        row["metadata_json"]["project_scope"] = [self.project]  # type: ignore[index]
        with without_insert_floor(), user_connection(self.urls["app"], self.user) as conn:
            created = _store(conn).create_open_loop(row, actor_type="user")
        self.loops[name] = str(created["id"])
        return str(created["id"])

    def stored(self) -> dict[str, tuple[str, str]]:
        with user_connection(self.urls["app"], self.user) as conn:
            found = {}
            for table, rows in (("generated_artifacts", self.artifacts), ("memories", self.memories), ("open_loops", self.loops)):
                for name, row_id in rows.items():
                    row = conn.execute(f"SELECT domain, sensitivity FROM {table} WHERE id = %s", (row_id,)).fetchone()  # closed table names
                    found[f"{table}:{name}"] = (row["domain"], row["sensitivity"])
            return found

    def below(self) -> list[str]:
        """Ids of derived rows below their inputs, by the same kernel the doctor uses."""
        with user_connection(self.urls["app"], self.user) as conn:
            below, _unverified = classify_stored_labels(load_postgres_label_tables(conn))
        return [row_id for _table, _user, row_id, *_ in below]

    def unverified(self) -> dict[str, list[str]]:
        with user_connection(self.urls["app"], self.user) as conn:
            _below, unverified = classify_stored_labels(load_postgres_label_tables(conn))
        return unverified


def _store(conn):
    from alicebot_api.vnext_store import PostgresVNextStore

    return PostgresVNextStore(conn)


def v020_database(urls: dict[str, str], monkeypatch) -> Scene:
    command.upgrade(make_alembic_config(urls["admin"]), "20260721_0094")
    return Scene(urls)


def wire(urls: dict[str, str], monkeypatch, scene: Scene) -> LabelHarness:
    """Point the mounted application at this database, as the ``label_harness`` fixture does for a migrated template."""
    from alicebot_api import vnext_label_writes
    from alicebot_api.routers import vnext_memories, vnext_projects, vnext_retrieval, vnext_review, workspaces

    settings = Settings(database_url=urls["app"])
    for module in (main_module, vnext_memories, vnext_projects, vnext_retrieval, vnext_review, workspaces):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_label_writes, "STRICT_LOCK_ORDER", True)
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    return LabelHarness(urls, scene.user)


def keys_for(harness: LabelHarness, scene: Scene) -> dict[str, str]:
    return {
        name: harness.key(profile, project=scene.project if bound else None) for name, (profile, bound) in PROFILES.items()
    }


def artifact_status(harness: LabelHarness, key: str | None, artifact_id: str) -> tuple[int, object]:
    if key is None:
        return owner_artifact(harness, artifact_id)
    status, body, _headers = harness.request("GET", f"/v0/vnext/artifacts/{artifact_id}", key=key)
    return status, body


def assert_reads(harness, keys, artifact_id, allowed: dict[str, bool], *, label: str, owner: bool = True) -> None:
    """Every key answers 200 with the report where ``allowed`` says so, and a 404 that names nothing of it everywhere else."""
    if owner:
        with keyless_owner(harness):
            status, body = owner_artifact(harness, artifact_id)
        assert status == 200 and body["id"] == artifact_id, (label, "owner", status)
    for name, key in keys.items():
        status, body = artifact_status(harness, key, artifact_id)
        text = json.dumps(body, default=str)
        if allowed.get(name, False):
            assert status == 200 and body["id"] == artifact_id, (label, name, status, text[:200])
        else:
            assert status == 404, (label, name, status, text[:200])
            assert SENTINEL not in text and artifact_id not in text, (label, name)


# The keys read for every shape: the unbound and the bound admin key (which read a raised report), a key below the memory with
# no binding and one bound to the project. The single-report tests read with all eight.
SHAPE_KEYS = ("admin", "admin_bound", "read_only", "alpha_only")


def assert_every_report(harness, scene, keys, *, label: str) -> None:
    """The owner (once, with every key revoked) and ``SHAPE_KEYS`` read each report as its outcome says."""
    with keyless_owner(harness):
        for name, artifact_id in scene.artifacts.items():
            status, body = owner_artifact(harness, artifact_id)
            assert status == 200 and body["id"] == artifact_id, (label, name, "owner", status)
    some = {name: keys[name] for name in SHAPE_KEYS}
    for name, artifact_id in scene.artifacts.items():
        assert_reads(harness, some, artifact_id, expected_reads(scene, name, keys), label=f"{label}: {name}", owner=False)


# --- the reported row -----------------------------------------------------------------------------------------------------


def seed_reported(scene: Scene) -> str:
    """The v0.20.0 consolidation report that cites a confidential memory as JSON text, stored ``internal``."""
    entry = json.dumps({"memory_id": scene.memory, "quote": SENTINEL})
    return scene.add_report("reported", [f"source:{scene.public_source}", entry])


def test_the_reported_report_is_raised_by_the_upgrade_and_hidden_from_keys_below_the_memory(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    artifact = seed_reported(scene)
    assert scene.stored()["generated_artifacts:reported"] == ("unknown", "internal")
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    assert_roles_and_force(urls)
    assert scene.stored()["generated_artifacts:reported"] == RAISED
    assert scene.below() == []
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    assert_reads(harness, keys, artifact, READS_RAISED, label="raised")
    with user_connection(urls["app"], scene.user) as conn:
        events = conn.execute(
            "SELECT payload_json FROM event_log WHERE event_type = 'artifact.labels_raised' AND target_id = %s", (artifact,)
        ).fetchall()
    assert len(events) == 1 and events[0]["payload_json"]["new"]["sensitivity"] == "confidential"
    assert SENTINEL not in json.dumps(events, default=str)


def test_redacting_the_cited_memory_hides_the_report_until_one_is_made_without_the_citation(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    artifact = seed_reported(scene)
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    status, body, _ = harness.request(
        "POST", "/v0/vnext/memories/redact", payload={"memory_id": scene.memory, "reason": "synthetic"}, key=keys["admin"]
    )
    assert status == 200, body
    # Unverified: the owner and the unbound admin key read it, no other key does, and the bound admin key is refused too.
    assert_reads(harness, keys, artifact, READS_UNVERIFIED, label="redacted")
    assert set(scene.unverified()) == {"input_redacted"} and scene.unverified()["input_redacted"] == [artifact]
    # Regenerated without the citation: a new report is readable by a key below the memory, and the old one stays hidden.
    fresh = scene.add_report("regenerated", [f"source:{scene.public_source}"])
    for name in ("trusted", "alpha_only", "read_only"):
        status, body = artifact_status(harness, keys[name], fresh)
        assert status == 200, (name, status, body)
    assert_reads(harness, keys, artifact, READS_UNVERIFIED, label="redacted, after regenerating")


def test_a_lower_key_is_refused_by_the_door_the_memory_itself_is_refused_by(database_urls, monkeypatch):
    """The report answers as the memory does: a key that is told the memory does not exist is told the same of the report."""
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    artifact = seed_reported(scene)
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    for name in ("alpha_only", "read_only", "trusted"):
        report_status, _ = artifact_status(harness, keys[name], artifact)
        memory_status, _body, _ = harness.request("GET", f"/v0/vnext/memories/{scene.memory}/audit", key=keys[name])
        assert report_status == 404
        assert memory_status in {403, 404}, (name, memory_status)


# --- every shape ---------------------------------------------------------------------------------------------------------

# Every shape at the canonical spelling, and every spelling for the four that carry most citations. The cross product of shape
# and spelling is stated on both stores without a database in ``test_legacy_citation_shapes.py`` and
# ``test_sqlite_legacy_citations.py``.
SPELLED = {"prefix", "json-object", "url", "prefix-fragment"}
MEMORY_SHAPE_CASES = [
    (shape, spelling)
    for shape in memory_shapes("q")
    if shape != "json-repeated-key"
    for spelling in (list(SPELLINGS) if shape in SPELLED else ["canonical"])
]
SOURCE_SHAPE_CASES = [
    (shape, spelling)
    for shape in source_shapes("q")
    if shape != "json-in-json"
    for spelling in (list(SPELLINGS) if shape in SPELLED else ["canonical"])
    if not (spelling == "padded" and shape == "url")
]
# The cases of the test that reads a database nobody repaired, where every read computes the closure again.
SHORT_MEMORY = [("prefix", "canonical"), ("prefix-capitals", "canonical"), ("url", "urn"), ("json-object", "canonical"),
                ("json-list", "capitals"), ("json-ids", "braces"), ("json-in-json", "canonical"), ("json-object", "no-hyphens"),
                ("prefix-fragment", "braces")]
SHORT_SOURCE = [("prefix", "canonical"), ("url", "capitals"), ("json-object", "canonical"), ("bare", "no-hyphens")]


def seed_every_shape(scene: Scene, *, short: bool = False) -> None:
    for shape, spelling in SHORT_MEMORY if short else MEMORY_SHAPE_CASES:
        scene.add_report(f"memory-{shape}-{spelling}", [_memory_entry(shape, spelling, scene.memory)])
    for shape, spelling in SHORT_SOURCE if short else SOURCE_SHAPE_CASES:
        scene.add_report(f"source-{shape}-{spelling}", [_source_entry(shape, spelling, scene.source)])
    for position in ("proposal-source_refs", "agentic_memory", "provenance", "replacement_provenance"):
        scene.add_report(f"position-{position}", [_memory_entry("json-object", "canonical", scene.memory)], position=position)
    for position in ("source_refs", "agentic_memory", "value"):
        scene.add_candidate(f"memory-{position}", [_memory_entry("json-object", "canonical", scene.memory)], position=position)
    scene.add_loop("memory-json", [_memory_entry("json-object", "canonical", scene.memory)])


def expected_reads(scene: Scene, name: str, keys: dict[str, str]) -> dict[str, bool]:
    outcome = scene.outcome.get(name, "unchanged")
    if outcome == "raised":
        return READS_RAISED
    if outcome == "unverified":
        return READS_UNVERIFIED
    return {key: True for key in keys}


SWEEP_INCIDENTAL = ("memory-json-origin", "memory-sentence-space", "memory-mem-prefix", "memory-json-cut", "memory-object-origin",
                    "source-outside-url", "source-list-repr")


def seed_incidental_and_calm(scene: Scene, *, few: bool = False) -> None:
    """Wordings no marker covers (raised, as the reader finds the id), and syntax or ids that name nothing confidential (not)."""
    corpus = corpus_entries(scene.memory, scene.source, SENTINEL)
    for name in SWEEP_INCIDENTAL if few else INCIDENTAL_NAMES:
        scene.add_report(f"incidental-{name}", [corpus[name]])
    ghost = str(uuid4())
    public = scene.public_source
    for name, entry in (
        ("list-repr", f"['{public}']"),
        ("markdown-link", f"[the note](alice://sources/{public})"),
        ("cut-json", '{"source_id": "%s", "quote": "cut off in the middle' % public),
        ("trailing-comma", '{"source_id": "%s",}' % public),
        ("bracket-title", "[1] Smith 2024, a long enough title with no id in it at all"),
        ("ghost-sentence", f"copied from memory: {ghost} on Monday"),
        ("ghost-json", '{"memory_id": "%s", "quote": "cut' % ghost),
        ("ghost-url", f"https://host.example/memories/{ghost}"),
    ):
        scene.add_report(f"calm-{name}", [entry])
    # An id that is not a citation at all, in a field that is not a reference: a trace id, a run id.
    row = report([f"source:{public}"])
    row["metadata_json"]["project_scope"] = [scene.project]  # type: ignore[index]
    row["metadata_json"]["agent_run_id"] = f"memory:{ghost}"  # type: ignore[index]
    row["metadata_json"]["trace_id"] = scene.memory  # type: ignore[index]
    with without_insert_floor(), user_connection(scene.urls["app"], scene.user) as conn:
        created = _store(conn).create_artifact(row)
    scene.artifacts["calm-run-id"] = str(created["id"])
    scene.outcome["calm-run-id"] = "unchanged"
    # Words that mark themselves as a citation and hold the confidential memory in a spelling the scan cannot take apart.
    for name, entry in (
        ("odd-fragment", f"memory:{odd_hyphens(scene.memory)}#chunk-1"),
        ("odd-url", f"alice://memories/{odd_hyphens(scene.memory)}#section"),
    ):
        scene.add_report(f"unreadable-{name}", [entry], unreadable=True)
    # The same words naming no stored row do not hold a row back.
    scene.add_report("calm-odd-ghost", [f"memory:{odd_hyphens(ghost)}#chunk-1"])


def test_every_shape_is_raised_by_the_upgrade_and_no_restricted_key_reads_any_of_them(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    seed_every_shape(scene)
    seed_incidental_and_calm(scene)
    scene.add_report("plain", [f"source:{scene.public_source}"])
    outcomes = list(scene.outcome.values())
    # The corpus is not vacuous: most shapes are raised, two are unverified, and the ones that name nothing stay.
    assert outcomes.count("raised") > 80 and outcomes.count("unverified") == 2 and outcomes.count("unchanged") >= 10, {
        name: outcomes.count(name) for name in ("raised", "unverified", "unchanged")
    }
    assert all(scene.outcome[f"calm-{name}"] == "unchanged" for name in ("list-repr", "markdown-link", "cut-json", "trailing-comma",
                                                                         "ghost-sentence", "ghost-json", "odd-ghost", "run-id"))
    assert all(scene.outcome[f"incidental-{name}"] == "raised" for name in INCIDENTAL_NAMES)
    before = scene.stored()
    assert all(label[1] == "internal" for label in before.values())
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    after = scene.stored()
    for name, outcome in scene.outcome.items():
        label = after[f"generated_artifacts:{name}"]
        assert label == (RAISED if outcome == "raised" else ("unknown", "internal")), (name, outcome, label)
    for name in list(scene.memories):
        assert after[f"memories:{name}"] == RAISED, name
    assert after["open_loops:memory-json"] == RAISED
    assert scene.below() == []
    unverified = scene.unverified()
    assert set(unverified) == {"unreadable_citation"}
    assert sorted(unverified["unreadable_citation"]) == sorted(scene.artifacts[name] for name in scene.unreadable)
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    assert_every_report(harness, scene, keys, label="upgraded")


def assert_stored_as_the_reader_says(scene: Scene) -> None:
    """A report the reader finds the confidential memory or source in is raised, and every other report stays where it was."""
    stored = scene.stored()
    for name, outcome in scene.outcome.items():
        assert stored[f"generated_artifacts:{name}"] == (RAISED if outcome == "raised" else ("unknown", "internal")), (name, outcome)


def test_a_second_upgrade_and_a_repeat_of_the_repair_change_nothing(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    seed_every_shape(scene)
    config = make_alembic_config(urls["admin"])
    command.upgrade(config, "head")
    first = scene.stored()
    command.downgrade(config, "20261004_0095")
    command.upgrade(config, "head")
    assert scene.stored() == first
    ctx = SimpleNamespace(database_url=urls["app"], user_id=scene.user)
    assert labels._run_vnext_labels_repair(ctx, None) == "labels repair updated 0"
    assert scene.below() == []


# --- revision 0097 ------------------------------------------------------------------------------------------------------


@contextmanager
def narrow_reading(monkeypatch):
    """The closure as it read before: the plain ``memory:<id>`` and ``source:<id>`` strings of ``source_refs`` and nothing else,
    with no candidate and no unreadable word."""
    plain = re.compile(r"^(memory|source):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
    original = vnext_derived_labels._dependency_uncached

    def old(kind, row):
        full = original(kind, row)
        if not vnext_derived_labels.is_derived(kind, row):
            return full
        meta = vnext_derived_labels._metadata(row)
        refs = meta.get("source_refs") if isinstance(meta.get("source_refs"), list) else []
        found = {(m.group(1), m.group(2)) for ref in refs if isinstance(ref, str) and (m := plain.match(ref))}
        return frozenset(found), "", frozenset(), frozenset()

    with monkeypatch.context() as patch:
        patch.setattr(vnext_derived_labels, "_dependency_uncached", old)
        yield


def test_revision_0097_repairs_a_database_that_already_ran_0096_with_the_narrow_reading(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    artifact = seed_reported(scene)
    config = make_alembic_config(urls["admin"])
    with narrow_reading(monkeypatch):
        command.upgrade(config, "20261005_0096")
    with psycopg.connect(urls["admin"]) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261005_0096"
    assert scene.stored()["generated_artifacts:reported"] == ("unknown", "internal"), "0096 left the report where it was"
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    status, _body = artifact_status(harness, keys["alpha_only"], artifact)
    assert status == 404, "the read check already holds the report to the memory"
    command.upgrade(config, "head")
    with psycopg.connect(urls["admin"]) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261011_0097"
    assert scene.stored()["generated_artifacts:reported"] == RAISED
    assert scene.below() == []
    assert_roles_and_force(urls)
    with user_connection(urls["app"], scene.user) as conn:
        cause = conn.execute(
            "SELECT payload_json->>'cause' AS cause FROM event_log WHERE event_type = 'artifact.labels_raised' AND target_id = %s",
            (artifact,),
        ).fetchone()["cause"]
    assert cause == repair.REPAIR_CAUSE == "repair_v4"
    assert_reads(harness, keys, artifact, READS_RAISED, label="after 0097")


def test_revision_0097_chains_after_0096_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(make_alembic_config("postgresql://u:p@127.0.0.1:1/x"))
    assert script.get_heads() == ["20261011_0097"]
    assert script.get_revision("20261011_0097").down_revision == "20261005_0096"


def test_an_upgrade_that_fails_part_way_changes_nothing_and_the_rerun_leaves_no_row_below_its_inputs(database_urls, monkeypatch):
    urls = database_urls
    config = make_alembic_config(urls["admin"])
    command.upgrade(config, "20261004_0095")
    scene = Scene(urls)
    seed_every_shape(scene)
    seed_incidental_and_calm(scene)
    before = scene.stored()
    backup, restored = "alicebot_backup_" + uuid4().hex[:12], "alicebot_restored_" + uuid4().hex[:12]
    close_connection_pools()
    _create_role_separated_database(backup, template=urlsplit(urls["admin"]).path.lstrip("/"))
    try:
        original = repair.plan_label_repairs

        def interrupted(tables):
            # Alembic iterates this and really writes the first three rows before the iterator raises.
            for index, change in enumerate(original(tables)):
                if index == 3:
                    raise RuntimeError("injected after three migration writes")
                yield change

        with monkeypatch.context() as patch:
            patch.setattr(repair, "plan_label_repairs", interrupted)
            with pytest.raises(RuntimeError, match="three migration writes"):
                command.upgrade(config, "head")
        assert scene.stored() == before
        assert_roles_and_force(urls)
        with psycopg.connect(urls["admin"]) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261004_0095"
        command.upgrade(config, "head")
        assert scene.below() == []
        assert scene.stored()["generated_artifacts:memory-json-object-canonical"] == RAISED
        assert scene.stored()["generated_artifacts:memory-prefix-fragment-braces"] == RAISED
        assert_stored_as_the_reader_says(scene)
        # A backup taken before the upgrade, restored and upgraded, ends the same way.
        close_connection_pools()
        restored_urls = _create_role_separated_database(restored, template=backup)
        try:
            copy = Scene.__new__(Scene)
            copy.urls, copy.user = restored_urls, scene.user
            copy.artifacts, copy.memories, copy.loops = scene.artifacts, scene.memories, scene.loops
            copy.unreadable = scene.unreadable
            assert copy.stored() == before
            command.upgrade(make_alembic_config(restored_urls["admin"]), "head")
            assert copy.below() == []
            assert copy.stored() == scene.stored()
            assert_roles_and_force(restored_urls)
        finally:
            close_connection_pools()
            _drop_database(restored)
    finally:
        _drop_database(backup)


def test_a_0097_that_fails_part_way_changes_nothing_and_the_rerun_finishes(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    seed_every_shape(scene)
    config = make_alembic_config(urls["admin"])
    with narrow_reading(monkeypatch):
        command.upgrade(config, "20261005_0096")
    before = scene.stored()
    original = repair.plan_label_repairs

    def interrupted(tables):
        for index, change in enumerate(original(tables)):
            if index == 5:
                raise RuntimeError("injected after five migration writes")
            yield change

    with monkeypatch.context() as patch:
        patch.setattr(repair, "plan_label_repairs", interrupted)
        with pytest.raises(RuntimeError, match="five migration writes"):
            command.upgrade(config, "head")
    assert scene.stored() == before
    assert_roles_and_force(urls)
    command.upgrade(config, "head")
    assert scene.below() == []
    assert_roles_and_force(urls)


# --- a row that was never repaired ---------------------------------------------------------------------------------------


def test_a_row_that_was_never_repaired_is_read_at_the_label_of_its_inputs_and_labels_repair_raises_it(
    migrated_database_urls, monkeypatch
):
    """A backup restored at the head: the schema is current, the stored labels are not, and no migration will run."""
    urls = migrated_database_urls
    scene = Scene(urls)
    seed_every_shape(scene, short=True)
    seed_reported(scene)
    seed_incidental_and_calm(scene, few=True)
    assert scene.stored()["generated_artifacts:reported"] == ("unknown", "internal")
    assert len(scene.below()) > 20
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    assert_every_report(harness, scene, keys, label="before the repair")
    ctx = SimpleNamespace(database_url=urls["app"], user_id=scene.user)
    with pytest.raises(SystemExit):
        labels._run_vnext_labels_check(ctx, None)
    assert labels._run_vnext_labels_repair(ctx, None).startswith("labels repair updated ")
    assert scene.below() == []
    assert scene.stored()["generated_artifacts:reported"] == RAISED
    assert all(scene.stored()[f"generated_artifacts:{name}"] == RAISED for name, outcome in scene.outcome.items() if outcome == "raised")
    with user_connection(urls["app"], scene.user) as conn:
        causes = {row["cause"] for row in conn.execute(
            "SELECT payload_json->>'cause' AS cause FROM event_log WHERE event_type LIKE '%.labels_raised'"
        )}
    assert causes == {"repair_v4"}
    assert_every_report(harness, scene, keys, label="after the repair")


def test_a_relabelled_memory_reaches_a_legacy_report_that_cites_it_in_json_text(migrated_database_urls, monkeypatch):
    """The reverse lookup finds the report through the same closure, so lowering and raising the memory follows."""
    urls = migrated_database_urls
    scene = Scene(urls)
    seed_reported(scene)
    incidental = corpus_entries(scene.memory, scene.source, SENTINEL)["memory-sentence-space"]
    scene.add_report("incidental", [incidental])
    scene.add_report("calm", [f"copied from memory: {uuid4()} on Monday"])
    harness = wire(urls, monkeypatch, scene)
    ctx = SimpleNamespace(database_url=urls["app"], user_id=scene.user)
    labels._run_vnext_labels_repair(ctx, None)
    assert scene.stored()["generated_artifacts:reported"] == RAISED
    assert scene.stored()["generated_artifacts:incidental"] == RAISED
    assert scene.stored()["generated_artifacts:calm"] == ("unknown", "internal")
    status, body, _ = harness.relabel("memory", scene.memory, sensitivity="highly_sensitive")
    assert status == 200, body
    assert scene.stored()["generated_artifacts:reported"][1] == "highly_sensitive"
    assert scene.stored()["generated_artifacts:incidental"][1] == "highly_sensitive", "the reverse lookup finds an incidental id"
    assert scene.stored()["generated_artifacts:calm"] == ("unknown", "internal")


# --- every door ------------------------------------------------------------------------------------------------------------


def stage_for_the_sweep(scene: Scene) -> None:
    for shape, spelling in (("prefix", "canonical"), ("prefix-capitals", "canonical"), ("url", "canonical"), ("json-object", "canonical"),
                            ("json-list", "capitals"), ("json-in-json", "canonical"), ("prefix", "no-hyphens"), ("json-ids", "braces")):
        scene.add_report(f"memory-{shape}-{spelling}", [_memory_entry(shape, spelling, scene.memory)])
    scene.add_candidate("memory-json", [_memory_entry("json-object", "canonical", scene.memory)])
    scene.add_loop("memory-json", [_memory_entry("json-object", "canonical", scene.memory)])
    seed_incidental_and_calm(scene, few=True)


def _sweep_scene(scene: Scene) -> SimpleNamespace:
    return SimpleNamespace(alpha=scene.project, sources=[{"id": scene.source}, {"id": scene.public_source}])


SWEEP_KEYS = ("trusted", "trusted_bound", "read_only", "alpha_only")


def raised_ids(scene: Scene, *, of_the_memory: bool = False) -> list[str]:
    """The reports that cite the confidential memory or source (or only the memory). The others cite nothing confidential and
    stay readable; the ones that cite only the source do not depend on the memory."""
    return [
        scene.artifacts[name]
        for name, outcome in scene.outcome.items()
        if outcome == "raised" and (not of_the_memory or name in scene.cites_memory)
    ]


def test_no_door_gives_a_restricted_key_the_quote_of_a_memory_it_may_not_read(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    stage_for_the_sweep(scene)
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    responses = sweep(harness, _sweep_scene(scene), monkeypatch, {name: keys[name] for name in SWEEP_KEYS})
    assert len(responses) > 150, "the sweep reaches every door"
    assert leaks(responses, SENTINEL) == [], leaks(responses, SENTINEL)[:5]
    for artifact_id in raised_ids(scene):
        assert not [item for item in responses if artifact_id in item.text], artifact_id
    # The control: the unbound admin key reads the same rows with the quote, so the sweep looks at something.
    control = sweep(harness, _sweep_scene(scene), monkeypatch, {"admin": keys["admin"]})
    assert leaks(control, SENTINEL), "the unbound admin key reads the quote at the doors"
    gets = [item for item in control if item.door == "artifact get" and item.status == 200]
    assert len(gets) == len(scene.artifacts)


def test_after_redaction_no_door_gives_any_key_but_the_owner_and_the_unbound_admin_key_the_quote(database_urls, monkeypatch):
    urls = database_urls
    scene = v020_database(urls, monkeypatch)
    stage_for_the_sweep(scene)
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    status, body, _ = harness.request("POST", "/v0/vnext/memories/redact", payload={"memory_id": scene.memory, "reason": "r"}, key=keys["admin"])
    assert status == 200, body
    restricted = {name: keys[name] for name in SWEEP_KEYS + ("admin_bound",)}
    responses = sweep(harness, _sweep_scene(scene), monkeypatch, restricted)
    assert leaks(responses, SENTINEL) == [], leaks(responses, SENTINEL)[:5]
    for artifact_id in raised_ids(scene, of_the_memory=True):
        assert not [item for item in responses if artifact_id in item.text and item.status == 200], artifact_id
    for artifact_id in raised_ids(scene):
        assert not [item for item in responses if artifact_id in item.text and item.status == 200 and item.key_name != "admin_bound"], artifact_id
    control = sweep(harness, _sweep_scene(scene), monkeypatch, {"admin": keys["admin"]})
    gets = [item for item in control if item.door == "artifact get" and item.status == 200]
    assert len(gets) == len(scene.artifacts)
    # The reports of the memory are unverified now it is redacted, and so are the two that hold it in a word nothing can take apart.
    assert set(scene.unverified()) == {"input_redacted", "unreadable_citation"}


def test_no_door_gives_a_restricted_key_the_quote_when_nothing_was_repaired(migrated_database_urls, monkeypatch):
    """A backup restored at the head: every door judges each report by the rows it really cites, the ids it only mentions and
    the ids that name nothing included."""
    urls = migrated_database_urls
    scene = Scene(urls)
    stage_for_the_sweep(scene)
    assert len(scene.below()) > 10
    harness = wire(urls, monkeypatch, scene)
    keys = keys_for(harness, scene)
    responses = sweep(harness, _sweep_scene(scene), monkeypatch, {name: keys[name] for name in SWEEP_KEYS})
    assert leaks(responses, SENTINEL) == [], leaks(responses, SENTINEL)[:5]
    raised = {scene.artifacts[name] for name, outcome in scene.outcome.items() if outcome == "raised"}
    calm = {scene.artifacts[name] for name, outcome in scene.outcome.items() if outcome == "unchanged"}
    assert raised and calm
    for artifact_id in raised:
        assert not [item for item in responses if artifact_id in item.text], artifact_id
    # The reports that cite nothing confidential stay readable: the sweep does not hide everything.
    gets = {item.body.get("id") for item in responses if item.door == "artifact get" and item.status == 200 and isinstance(item.body, dict)}
    assert calm <= gets, "a report that cites nothing confidential is read by a key below the memory"


# --- the insert floor -----------------------------------------------------------------------------------------------------


def test_the_insert_floor_resolves_what_a_new_report_cites_in_any_wording(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    scene = Scene(urls)
    harness = wire(urls, monkeypatch, scene)
    corpus = corpus_entries(scene.memory, scene.source, SENTINEL)
    ghost = str(uuid4())
    written = {
        "plain": ([f"source:{scene.public_source}"], "unchanged"),
        "json-object": ([_memory_entry("json-object", "canonical", scene.memory)], "raised"),
        "origin": ([corpus["memory-json-origin"]], "raised"),
        "sentence": ([corpus["memory-sentence-space"]], "raised"),
        "mem-prefix": ([corpus["memory-mem-prefix"]], "raised"),
        "source-url": ([corpus["source-outside-url"]], "raised"),
        "markdown-ghost": ([f"[the note](alice://memories/{ghost})"], "unchanged"),
        "list-repr": ([f"['{scene.public_source}']"], "unchanged"),
        "ghost-sentence": ([f"copied from memory: {ghost} on Monday"], "unchanged"),
        "odd-unreadable": ([f"memory:{odd_hyphens(scene.memory)}#chunk-1"], "unverified"),
        "odd-ghost": ([f"memory:{odd_hyphens(ghost)}#chunk-1"], "unchanged"),
    }
    with harness.store() as store:
        for name, (entries, _outcome) in written.items():
            row = report(entries)
            row["metadata_json"]["project_scope"] = [scene.project]  # type: ignore[index]
            scene.artifacts[name] = str(store.create_artifact(row)["id"])
    stored = scene.stored()
    for name, (_entries, outcome) in written.items():
        label = stored[f"generated_artifacts:{name}"]
        if outcome == "raised":
            assert label == RAISED, (name, label)
        elif outcome == "unverified":
            assert label[1] == "regulated", (name, label)
        else:
            assert label == ("unknown", "internal"), (name, label)
    assert scene.below() == []
    assert set(scene.unverified()) <= {"unreadable_citation"}
