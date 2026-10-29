"""A SQLite vault written before derived labels existed holds citations in every shape, and the open pass reads them all.

The vault here is what the version before derived labels left: a confidential memory, derived memories and open loops that
cite it in each shape a client could send (see ``legacy_citation_support``), stored at the label they were made with, no
repair state in ``alice_schema_state``. Opening it runs the repair once. These tests ask:

* the open pass raises every row that cites the memory in a shape the reader names, whatever the spelling and the position;
* a row whose citation cannot be read is not rewritten and is unverified, so only the owner and an unbound admin key read it;
* a vault that an earlier build stamped as repaired is repaired again, because its pass read fewer shapes;
* an open pass that fails part way changes nothing, and the next open finishes it;
* a row that was never repaired (a vault restored from an old backup and read first) is read at the label of its inputs, by
  real keys at the MCP doors, and the owner and an unbound admin key still read it;
* a redacted memory takes the report that cites it out of every key but those two.

Mutations (``scripts/derived_label_mutations.json`` replays each): in ``_derived_dependency_record`` drop ``found |= citations``;
set ``REPAIR_STATE_KEY`` back to ``derived_labels_v3``; in ``_derived_dependency_record`` drop the ``unreadable_citation`` problem.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import UUID, uuid4

import pytest

from alicebot_api import sqlite_schema
from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_label_repair import (
    REPAIR_CAUSE,
    REPAIR_STATE_KEY,
    _load_tables,
    classify_stored_labels,
    relabel_labels_sqlite,
)
from alicebot_api.vnext_label_writes import walk_dependants, without_insert_floor
from tests.unit.legacy_citation_support import (
    INCIDENTAL_NAMES,
    SPELLINGS,
    corpus_entries,
    memory_shapes,
    odd_hyphens,
    source_shapes,
    typed_object_entries,
)

USER = str(UUID("00000000-0000-4000-8000-0000000000a1"))
SENTINEL = "Cedar kiln code 4417"
PUBLIC_WORDS = "the kiln is fired on Mondays"
# Shapes the reader names in a memory ref, at each spelling of the id, plus the sources.
MEMORY_SHAPES = memory_shapes(SENTINEL)
SOURCE_SHAPES = source_shapes(SENTINEL)
# Words that mark themselves as a citation and hold an id with its hyphens in places ``uuid.UUID`` takes and the reader's pattern
# does not. The scan cannot take them apart, so the row is held back when the id names a stored row.
UNREADABLE = {
    "odd-fragment": "memory:%s#chunk-1",
    "odd-url": "alice://memories/%s#section",
}


# Object entries the closure has always read as the kind they name (an artifact has no table in a vault).
OBJECT_NAMES = (
    "open_loop-typed-object-ref", "open_loop-typed-object-origin", "open_loop-typed-object-text", "open_loop-typed-object-title",
    "memory-typed-object-summary", "source-typed-object-quote", "source-excerpt-bare", "source-excerpt-json", "source-excerpt-prefix",
)


def _cases(memory_id: str, source_id: str) -> dict[str, tuple[str, object]]:
    """name -> (what it cites, the entry). Every shape at the canonical spelling, every spelling for two shapes."""

    cases: dict[str, tuple[str, object]] = {}
    for shape, build in MEMORY_SHAPES.items():
        if shape == "json-repeated-key":
            continue  # it names a second id on purpose, and the unit tests state what that does
        cases[f"memory-{shape}"] = ("memory", build(memory_id))
    for spelling, spell in SPELLINGS.items():
        for shape in ("prefix", "json-object", "url"):
            cases[f"memory-{shape}-{spelling}"] = ("memory", MEMORY_SHAPES[shape](spell(memory_id)))
    for shape, build in SOURCE_SHAPES.items():
        if shape == "json-in-json":
            continue  # the reader reads this one as incidental
        cases[f"source-{shape}"] = ("source", build(source_id))
    for spelling, spell in SPELLINGS.items():
        for shape in ("prefix", "json-object"):
            if spelling == "padded" and shape == "prefix":
                continue
            cases[f"source-{shape}-{spelling}"] = ("source", SOURCE_SHAPES[shape](spell(source_id)))
    return cases


def _derived_memory(name: str, entry: object, *, position: str = "metadata", words: str = SENTINEL) -> dict[str, object]:
    metadata: dict[str, object] = {"candidate_kind": "memory_rollup", "project_scope": []}
    value: dict[str, object] = {"kind": "rollup_card", "text": f"Roll-up {name}: {words}"}
    if position == "metadata":
        metadata["source_refs"] = [entry]
    elif position == "agentic":
        metadata["agentic_memory"] = {"source_refs": [entry]}
    else:
        value["source_refs"] = [entry]
    return {
        "memory_key": f"rollup.{name}",
        "memory_type": "semantic",
        "title": f"Roll-up {name}",
        "canonical_text": f"Roll-up {name}: {words}",
        "status": "active",
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": metadata,
        "value": value,
    }


class Vault:
    """A vault as the version before derived labels left it, and the rows to ask about."""

    def __init__(
        self, path, monkeypatch, *, stamp: str | None = None, unreadable: bool = True, memory_label: tuple[str, str] = ("health", "confidential")
    ) -> None:
        self.path = path
        self.monkeypatch = monkeypatch
        self.rows: dict[str, tuple[str, str]] = {}  # name -> (table, id)
        self.cites: dict[str, str] = {}
        with monkeypatch.context() as patch:
            patch.setattr(sqlite_schema, "_relabel_derived_labels", lambda conn: None)
            bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
            with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
                store = SQLiteVNextStore(conn, USER)
                source = store.create_source(
                    {"source_type": "note", "title": "Clinic note", "content_hash": "sha256:" + uuid4().hex,
                     "domain": "health", "sensitivity": "confidential", "metadata_json": {}}
                )
                memory = store.create_memory(
                    {"memory_key": "clinic.code", "memory_type": "semantic", "canonical_text": SENTINEL, "title": "Kiln code",
                     "status": "active", "domain": memory_label[0], "sensitivity": memory_label[1], "value": {"text": SENTINEL}}
                )
                public = store.create_source(
                    {"source_type": "note", "title": "Public note", "content_hash": "sha256:" + uuid4().hex,
                     "domain": "project", "sensitivity": "public", "metadata_json": {}}
                )
                self.memory_id, self.source_id, self.public_id = str(memory["id"]), str(source["id"]), str(public["id"])
                for name, (kind, entry) in _cases(self.memory_id, self.source_id).items():
                    row = store.create_memory(_derived_memory(name, entry))
                    self.rows[name] = ("memories", str(row["id"]))
                    self.cites[name] = kind
                for position in ("agentic", "value"):
                    row = store.create_memory(_derived_memory(f"position-{position}", MEMORY_SHAPES["json-object"](self.memory_id), position=position))
                    self.rows[f"position-{position}"] = ("memories", str(row["id"]))
                    self.cites[f"position-{position}"] = "memory"
                loop = store.create_open_loop(
                    {"title": "Check the kiln", "status": "open", "domain": "project", "sensitivity": "public",
                     "source_id": self.public_id,
                     "metadata_json": {"discovered_by": "vnext_daily_brief", "source_id": self.public_id, "project_scope": [],
                                       "source_refs": [MEMORY_SHAPES["json-object"](self.memory_id)]}}
                )
                self.rows["loop"] = ("open_loops", str(loop["id"]))
                self.cites["loop"] = "memory"
                # What a regenerated report would hold: no citation of the confidential memory.
                clean = store.create_memory(_derived_memory("regenerated", f"source:{self.public_id}", words=PUBLIC_WORDS))
                self.rows["regenerated"] = ("memories", str(clean["id"]))
                corpus = corpus_entries(self.memory_id, self.source_id, SENTINEL)
                for name in INCIDENTAL_NAMES:
                    row = store.create_memory(_derived_memory(f"incidental-{name}", corpus[name]))
                    self.rows[f"incidental-{name}"] = ("memories", str(row["id"]))
                    self.cites[f"incidental-{name}"] = name.split("-", 1)[0]
                # Objects of source_refs that hold a typed ref under a key the saved-quote reader skips as text, or a source in a
                # conversation_excerpt: a confidential open loop and source are cited as the kind they are.
                confidential_loop = store.create_open_loop(
                    {"title": "Confidential follow up", "status": "open", "domain": "health", "sensitivity": "confidential",
                     "source_id": self.public_id,
                     "metadata_json": {"discovered_by": "vnext_daily_brief", "source_id": self.public_id, "project_scope": []}}
                )
                self.loop_id = str(confidential_loop["id"])
                objects = typed_object_entries(self.memory_id, self.source_id, str(uuid4()), self.loop_id, SENTINEL)
                for name in OBJECT_NAMES:
                    row = store.create_memory(_derived_memory(f"object-{name}", objects[name]))
                    self.rows[f"object-{name}"] = ("memories", str(row["id"]))
                    self.cites[f"object-{name}"] = name.split("-", 1)[0]
                # Syntax and ids that name nothing confidential: every one of them stays where it was stored.
                ghost = str(uuid4())
                for name, entry in (
                    ("list-repr", f"['{self.public_id}']"),
                    ("markdown-link", f"[the note](alice://sources/{self.public_id})"),
                    ("cut-json", '{"source_id": "%s", "quote": "cut off in the middle' % self.public_id),
                    ("trailing-comma", '{"source_id": "%s",}' % self.public_id),
                    ("bracket-title", "[1] Smith 2024, a long enough title with no id in it at all"),
                    ("ghost-sentence", f"copied from memory: {ghost} on Monday"),
                    ("ghost-json", '{"memory_id": "%s", "quote": "cut' % ghost),
                ):
                    row = store.create_memory(_derived_memory(f"calm-{name}", entry, words="A calm note that quotes nothing."))
                    self.rows[f"calm-{name}"] = ("memories", str(row["id"]))
                row = _derived_memory("calm-run-id", f"source:{self.public_id}", words="A calm note that quotes nothing.")
                row["metadata_json"]["agent_run_id"] = f"memory:{ghost}"  # type: ignore[index]
                self.rows["calm-run-id"] = ("memories", str(store.create_memory(row)["id"]))
                if unreadable:
                    for name, template in UNREADABLE.items():
                        row = store.create_memory(_derived_memory(f"unreadable-{name}", template % odd_hyphens(self.memory_id)))
                        self.rows[f"unreadable-{name}"] = ("memories", str(row["id"]))
                if stamp is not None:
                    conn.execute("INSERT OR REPLACE INTO alice_schema_state (key, value) VALUES (?, '1')", (stamp,))

    def open(self):
        """Open the vault as the product does, which runs the repair pass when its key is not stamped."""

        return sqlite_user_connection(self.path, USER)

    def _raw(self):
        """The file as it is, read without the open pass."""

        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    def labels(self) -> dict[str, tuple[str, str]]:
        with self._raw() as conn:
            found = {}
            for name, (table, row_id) in self.rows.items():
                row = conn.execute(f"SELECT domain, sensitivity FROM {table} WHERE id = ?", (row_id,)).fetchone()  # closed table names
                found[name] = (row["domain"], row["sensitivity"])
            return found

    def events(self) -> list[dict[str, object]]:
        with self._raw() as conn:
            rows = conn.execute(
                "SELECT target_id, payload_json FROM event_log WHERE event_type IN ('memory.labels_raised', 'open_loop.labels_raised')"
            ).fetchall()
            return [{"target_id": row["target_id"], "payload": json.loads(row["payload_json"])} for row in rows]

    def stamped(self, key: str = REPAIR_STATE_KEY) -> bool:
        with self._raw() as conn:
            return conn.execute("SELECT value FROM alice_schema_state WHERE key = ?", (key,)).fetchone() is not None


def _raised(vault: Vault) -> set[str]:
    return {name for name, label in vault.labels().items() if label == ("health", "confidential")}


def _held_still(vault: Vault) -> set[str]:
    """Rows the open pass leaves where they were: the one a regenerated report is, syntax, ghosts, and the unverified."""

    return {name for name in vault.rows if name == "regenerated" or name.startswith(("calm-", "unreadable-"))}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return Vault(tmp_path / "legacy.sqlite3", monkeypatch)


def test_the_vault_starts_below_the_memory_it_cites(vault) -> None:
    assert _raised(vault) == set()
    assert not vault.stamped() and not vault.stamped("derived_labels_v3")


def test_the_open_pass_raises_every_row_that_cites_the_memory_in_a_shape_the_reader_names(vault) -> None:
    with vault.open():
        pass
    expected = set(vault.rows) - _held_still(vault)
    assert _raised(vault) == expected
    labels = vault.labels()
    for name in _held_still(vault):
        assert labels[name] == ("project", "public"), name  # an unverified row is not rewritten, and the rest names nothing
    assert vault.stamped()


def test_the_open_pass_records_one_event_per_raised_row_and_no_text(vault) -> None:
    with vault.open():
        pass
    events = vault.events()
    ids = {row_id for table, row_id in (vault.rows[name] for name in _raised(vault))}
    assert {event["target_id"] for event in events} == ids
    assert len(events) == len(ids)
    for event in events:
        assert event["payload"]["cause"] == REPAIR_CAUSE
        assert event["payload"]["new"]["sensitivity"] == "confidential"
    blob = json.dumps(events)
    assert SENTINEL not in blob and vault.memory_id not in blob


def test_a_row_whose_citation_cannot_be_read_is_unverified_and_the_check_says_so(vault) -> None:
    with vault.open() as conn:
        below, unverified = classify_stored_labels(_load_tables(conn))
    assert below == []
    names = {name for name in vault.rows if name.startswith("unreadable-")}
    assert {row_id for ids in unverified.values() for row_id in ids} == {vault.rows[name][1] for name in names}
    assert set(unverified) == {"unreadable_citation"}
    assert len(names) == 2


def test_a_second_open_changes_nothing(vault) -> None:
    with vault.open():
        pass
    first = (vault.labels(), len(vault.events()))
    with vault.open():
        pass
    assert (vault.labels(), len(vault.events())) == first


def test_a_vault_that_an_earlier_build_stamped_is_repaired_again(tmp_path, monkeypatch) -> None:
    """The earlier pass read fewer shapes and stamped ``derived_labels_v3``. The key is new, so the pass runs once more."""

    vault = Vault(tmp_path / "stamped.sqlite3", monkeypatch, stamp="derived_labels_v3")
    assert REPAIR_STATE_KEY != "derived_labels_v3"
    assert _raised(vault) == set()
    with vault.open():
        pass
    assert "memory-json-object" in _raised(vault)
    assert vault.stamped()


def test_an_open_pass_that_fails_part_way_changes_nothing_and_the_next_open_finishes_it(vault, monkeypatch) -> None:
    from alicebot_api import vnext_label_repair

    original = vnext_label_repair.require_changed
    writes: list[str] = []

    def fail_third(count, table, row_id):
        writes.append(row_id)
        if len(writes) == 3:
            count = 0
        return original(count, table, row_id)

    with monkeypatch.context() as patch:
        patch.setattr(vnext_label_repair, "require_changed", fail_third)
        with vault.open():
            pass
    assert len(writes) == 3
    assert _raised(vault) == set()
    assert vault.events() == []
    assert not vault.stamped()
    with vault.open():
        pass
    assert _raised(vault) and vault.stamped()
    with vault.open() as conn:
        below, _unverified = classify_stored_labels(_load_tables(conn))
    assert below == []


def test_the_explicit_repair_finishes_what_an_open_left_and_leaves_no_row_below_its_inputs(vault) -> None:
    with sqlite_user_connection(vault.path, USER, repair_labels=False) as conn:
        changed = relabel_labels_sqlite(conn, explicit=True)
    assert changed == len(_raised(vault)) > 60
    with vault.open() as conn:
        below, _unverified = classify_stored_labels(_load_tables(conn))
    assert below == []
    assert _raised(vault) == set(vault.rows) - _held_still(vault)


def test_a_relabelled_memory_reaches_the_rows_that_cite_it_in_every_shape(vault) -> None:
    """The reverse lookup finds a dependant through the same closure, so a later relabel of the memory follows."""

    with vault.open() as conn:
        store = SQLiteVNextStore(conn, USER)
        found = {str(row["id"]) for row in walk_dependants(store, [vault.memory_id])}
    memory_rows = {vault.rows[name][1] for name, kind in vault.cites.items() if kind == "memory"}
    assert memory_rows <= found
    assert vault.rows["regenerated"][1] not in found


# --- reading a row that was never repaired ---------------------------------------------------------------------------


READERS = ("owner", "admin", "trusted", "read_only", "memory_proposal")
PROFILE = {"admin": "admin_agent", "trusted": "trusted_local_agent", "read_only": "read_only_agent",
           "memory_proposal": "memory_proposal_agent"}


def _setup_env(monkeypatch) -> None:
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)


def _keys(vault) -> dict[str, str | None]:
    """The readers. The owner's read ran keyless once; on a vault that holds
    keys a keyless call is refused, so the owner's leg runs as the owner's
    equal: an unbound admin_agent key gets what the owner got."""
    keys: dict[str, str | None] = {"owner": None}
    with vault.open() as conn:
        keys["owner"] = create_agent_key(SQLiteVNextStore(conn, USER), user_id=USER, agent_id="owner-equal", permission_profile="admin_agent", project_scope=None)[1]

    with vault.open() as conn:
        store = SQLiteVNextStore(conn, USER)
        for reader, profile in PROFILE.items():
            keys[reader] = create_agent_key(store, user_id=USER, agent_id=reader, permission_profile=profile)[1]
    return keys


def _door(path, monkeypatch, key, tool, arguments) -> str:
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
    try:
        return json.dumps(call_mcp_tool(context, name=tool, arguments=arguments), default=str)
    except MCPToolError as exc:
        return "tool-error: " + str(exc)


def _reads(vault, monkeypatch, key, name, *, quote_is_hidden: bool = True) -> list[tuple[str, bool]]:
    """For each door, whether the row ``name`` came back. When ``quote_is_hidden``, a door that did not return it may not
    hold the quote either (the rows that hold it are all restricted)."""

    row_id = vault.rows[name][1]
    answers = []
    for tool, arguments in (
        ("alice_recall", {"query": f"Roll-up {name}", "sensitivity_allowed": list(ALL_SENSITIVITY)}),
        ("alice_context_pack", {"query": f"Roll-up {name}", "sensitivity_allowed": list(ALL_SENSITIVITY)}),
        ("alice_explain", {"memory_id": row_id}),
    ):
        rendered = _door(vault.path, monkeypatch, key, tool, arguments)
        answers.append((tool, row_id in rendered and not rendered.startswith("tool-error")))
        if quote_is_hidden and not answers[-1][1]:
            assert SENTINEL not in rendered, (name, tool, rendered[:200])
    return answers


NAMES = ("memory-json-object", "memory-prefix", "memory-url-capitals", "memory-json-in-json", "position-value", "position-agentic",
         "source-json-object", "regenerated", "incidental-memory-json-origin", "incidental-memory-sentence-space",
         "incidental-source-outside-url", "object-open_loop-typed-object-ref", "object-source-excerpt-bare")
# Rows that cite nothing confidential: every key reads them, repaired or not.
CALM = ("calm-ghost-sentence", "calm-markdown-link", "calm-list-repr", "calm-cut-json", "calm-run-id")


@pytest.mark.parametrize("reader", READERS)
def test_a_row_that_was_never_repaired_is_read_at_the_label_of_its_inputs(tmp_path, monkeypatch, reader) -> None:
    _setup_env(monkeypatch)
    # Stamped as repaired, as a vault restored from an old backup is after its restore: the stored labels are still below.
    vault = Vault(tmp_path / "restored.sqlite3", monkeypatch, stamp=REPAIR_STATE_KEY, unreadable=False)
    assert _raised(vault) == set()
    key = _keys(vault)[reader]
    assert _raised(vault) == set(), "creating the keys did not repair anything"
    for name in NAMES:
        expected = reader in {"owner", "admin"} or name == "regenerated"
        assert [seen for _tool, seen in _reads(vault, monkeypatch, key, name)] == [expected] * 3, (reader, name)
    for name in CALM:
        assert [seen for _tool, seen in _reads(vault, monkeypatch, key, name)] == [True] * 3, (reader, name)


@pytest.mark.parametrize("reader", READERS)
def test_a_redacted_memory_takes_the_rows_that_cite_it_out_of_every_key_but_the_owner_and_the_admin_key(tmp_path, monkeypatch, reader) -> None:
    """The memory is public, so no ceiling hides the rows that cite it. Redacting it does, until they are regenerated."""

    _setup_env(monkeypatch)
    vault = Vault(tmp_path / "redacted.sqlite3", monkeypatch, unreadable=False, memory_label=("project", "public"))
    keys = _keys(vault)
    key = keys[reader]
    with vault.open():
        pass
    for name in NAMES:
        if name.startswith(("source-", "incidental-source", "object-")):
            continue  # it cites the confidential source or open loop, which hides it from the start
        assert [seen for _tool, seen in _reads(vault, monkeypatch, key, name, quote_is_hidden=False)] == [True] * 3, ("before", reader, name)
    with vault.open() as conn:
        redact_memory_flow(SQLiteVNextStore(conn, USER), memory_id=vault.memory_id, reason="synthetic")
    for name in NAMES:
        # The source it cites is confidential, and no key but those two reads a row that cites it.
        expected = reader in {"owner", "admin"} or name == "regenerated"
        assert [seen for _tool, seen in _reads(vault, monkeypatch, key, name)] == [expected] * 3, ("after", reader, name)
    with vault.open() as conn:
        below, unverified = classify_stored_labels(_load_tables(conn))
    cites_memory = {vault.rows[name][1] for name, kind in vault.cites.items() if kind == "memory"}
    assert below == [] and set(unverified) == {"input_redacted"}
    assert cites_memory == {row_id for ids in unverified.values() for row_id in ids}


def test_the_rows_that_cite_a_memory_are_regenerated_by_a_citation_free_copy(tmp_path, monkeypatch) -> None:
    """A copy made without the citation is readable again, and the old row stays restricted."""

    _setup_env(monkeypatch)
    vault = Vault(tmp_path / "regenerated.sqlite3", monkeypatch, unreadable=False, memory_label=("project", "public"))
    keys = _keys(vault)
    with vault.open() as conn:
        redact_memory_flow(SQLiteVNextStore(conn, USER), memory_id=vault.memory_id, reason="synthetic")
        store = SQLiteVNextStore(conn, USER)
        fresh = store.create_memory(_derived_memory("fresh", f"source:{vault.public_id}", words=PUBLIC_WORDS))
    vault.rows["fresh"] = ("memories", str(fresh["id"]))
    assert [seen for _tool, seen in _reads(vault, monkeypatch, keys["read_only"], "fresh")] == [True] * 3
    assert [seen for _tool, seen in _reads(vault, monkeypatch, keys["read_only"], "memory-json-object")] == [False] * 3
