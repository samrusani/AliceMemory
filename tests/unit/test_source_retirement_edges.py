"""Every graph edge that names a source is retired with the source, on SQLite.

``sources delete``, ``sources prune --superseded`` and ``import-markdown --supersede`` used to close the mention edges
that start at the source and nothing else. An edge of any other type, or one with the source at its other end, or one
that spelled the id in capitals, without hyphens, in braces or after ``urn:uuid:``, kept its explanation and metadata
and stayed readable to the keyless owner through ``alice_graph_neighborhood`` after the delete. The product's own SQLite
writers make only mention edges, but ``alice-memory import`` restores an edge of any shape, so the edge below reaches a
vault through a restore.

An edge that has the source (or one of its chunks) at an end is closed and its explanation and metadata are blanked.
An edge that names the source only in that text is blanked and keeps its link. The delete preview and the receipt count
the same edges (``graph_edges``).

Mutations, each alone, each named by the tests that fail (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``_edge_name_sets`` compares the ends as written instead of reading them with ``cited_source_ids``: the spelled-id
  shapes of ``test_every_shape_is_retired`` and ``test_the_finder_names_exactly_the_edges_that_name_the_source`` fail.
* ``retire_edges`` blanks the edges that have the source at an end but does not close them: the same tests fail on the
  open edge.
* ``retire_edges`` skips the edges that name the source only in their text: the ``text only`` shape fails.
* ``_close_edges`` keeps the explanation and metadata: every shape fails on the sentinel.
* ``_close_edges`` retires an entity only after a mention edge: ``test_an_entity_held_only_by_a_non_mention_edge_goes_too``.
* ``retire_dependents`` skips the edges of the candidates it redacts: ``test_edges_at_a_scrubbed_candidate_go_with_it``.
* ``retire_dependents`` retires no edge of the source: every shape fails, on all three commands.
* the chunk map is left empty: the ``chunk end`` shape fails.
* the edge scan and ``_EDGES_BY_ID`` both drop their ``user_id`` filter: ``test_another_users_edge_is_left_alone``.
* the preview leaves out ``graph_edges``: ``test_preview_and_receipt_count_the_same_edges``.
* the importer reads the edges once for every replaced file instead of once for the folder:
  ``test_a_folder_import_reads_the_edges_once``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import onramp
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import main as cli, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_stores.sqlite import source_retirement
from alicebot_api.vnext_stores.sqlite.source_retirement import edges_naming_sources
from tests.unit.test_importer_per_file_savepoint import USER_ID, _folder, _read, _vault
from tests.unit.test_source_commands import command
from tests.unit.test_source_supersede import run_import

NOTE = "Bob Smith keeps the kestrelplum lantern in the loft."
SENTINEL = "zebraplum"
KEPT = "zebrakeep"
MARKER = source_retirement.REMOVAL_MARKER


@pytest.fixture(autouse=True)
def _legacy_tools(monkeypatch):
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)


# Each shape is (kind, make). ``kind`` is how the finder must name the edge: at an end, or in its text only. ``make``
# gets the source id, one of its chunk ids, a memory id and an entity id and returns the five edge fields.
def _spelled(spelling):
    return lambda sid, chunk, mid, ent: ("source", spelling(sid), "memory", mid, "supports")


SHAPES = {
    "source to memory": ("ends", lambda sid, chunk, mid, ent: ("source", sid, "memory", mid, "supports")),
    "memory to source": ("ends", lambda sid, chunk, mid, ent: ("memory", mid, "source", sid, "supports")),
    "source to project": ("ends", lambda sid, chunk, mid, ent: ("source", sid, "project", "proj-1", "belongs_to_project")),
    "source to belief": ("ends", lambda sid, chunk, mid, ent: ("source", sid, "belief", str(uuid4()), "contradicts")),
    "source to entity, not a mention": ("ends", lambda sid, chunk, mid, ent: ("source", sid, "entity", ent, "supports")),
    "entity to source": ("ends", lambda sid, chunk, mid, ent: ("entity", ent, "source", sid, "mentions")),
    "capitals": ("ends", _spelled(str.upper)),
    "no hyphens": ("ends", _spelled(lambda sid: sid.replace("-", ""))),
    "braces": ("ends", _spelled(lambda sid: "{" + sid + "}")),
    "urn": ("ends", _spelled(lambda sid: "urn:uuid:" + sid)),
    "source prefix": ("ends", _spelled(lambda sid: "source:" + sid)),
    "source prefix and chunk suffix": ("ends", _spelled(lambda sid: f"source:{sid}#chunk-1")),
    "another from type": ("ends", lambda sid, chunk, mid, ent: ("document", sid, "memory", mid, "supports")),
    "chunk end": ("ends", lambda sid, chunk, mid, ent: ("source_chunk", chunk, "memory", mid, "supports")),
    "chunk end in capitals": ("ends", lambda sid, chunk, mid, ent: ("source_chunk", chunk.upper(), "memory", mid, "supports")),
    "text only": ("notes", lambda sid, chunk, mid, ent: ("memory", mid, "entity", ent, "supports")),
    "explanation only": ("notes explanation", lambda sid, chunk, mid, ent: ("memory", mid, "entity", ent, "supports")),
    "chunk in the metadata only": ("notes chunk", lambda sid, chunk, mid, ent: ("memory", mid, "entity", ent, "supports")),
    "chunk in the explanation only": (
        "notes chunk explanation", lambda sid, chunk, mid, ent: ("memory", mid, "entity", ent, "supports")),
}

# Where the text of a "notes" shape names the source, and whether it names a chunk of it.
NAMING = {"notes": ("source", "metadata"), "notes explanation": ("source", "explanation"),
          "notes chunk": ("chunk", "metadata"), "notes chunk explanation": ("chunk", "explanation")}


def _rows(db, sql, parameters=()):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, parameters).fetchall()


def _whole_database(db) -> str:
    pieces = []
    with closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            try:
                pieces.append(json.dumps([list(row) for row in conn.execute(f'SELECT * FROM "{name}"')], default=str))
            except sqlite3.DatabaseError:
                continue
    return "\n".join(pieces).casefold()


def _export_text(db) -> str:
    stream = io.StringIO()
    onramp._write_export(stream, db_path=db, user_id=UUID(USER_ID))
    return stream.getvalue().casefold()


def _tool(db, name, arguments):
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID))
    return call_mcp_tool(context, name=name, arguments=arguments)


def _neighbours(db, target):
    answer = _tool(db, "alice_graph_neighborhood", {"target_id": target})
    return [edge for edge in answer["from_edges"] + answer["to_edges"]]


def _edge(store, fields, label, *, names_source=None, names_in="metadata"):
    """One edge with the sentinel in its explanation and metadata. ``names_source`` puts that id (of a source or of one of
    its chunks) into the metadata, or into the explanation when ``names_in`` says so."""

    from_type, from_id, to_type, to_id, edge_type = fields
    metadata = {"why": f"{SENTINEL} metadata of {label}"}
    explanation = f"{SENTINEL} explanation of {label}"
    if names_source is not None and names_in == "metadata":
        metadata["connection"] = {"source_item": f"source:{names_source}"}
    elif names_source is not None:
        explanation += f" taken from source:{names_source}"
    return str(
        store.create_graph_edge(
            {
                "from_type": from_type, "from_id": from_id, "to_type": to_type, "to_id": to_id, "edge_type": edge_type,
                "confidence": 0.9, "explanation": explanation, "created_by": "vnext_connection_finder",
                "metadata_json": metadata,
            }
        )["id"]
    )


def _linker_entity(store, name):
    return str(
        store.create_entity(
            {
                "entity_type": "other", "name": name, "normalized_name": name.casefold(),
                "first_observed_at": "2030-01-01T00:00:00Z", "last_observed_at": "2030-01-01T00:00:00Z", "mention_count": 1,
                "metadata_json": {"created_by": "vnext_entity_linker"},
            }
        )["id"]
    )


@dataclass
class Seed:
    db: Path
    folder: Path
    sid: str
    mid: str
    entity: str
    named: str
    controls: dict
    kind: str
    mentions: list


def _seed(tmp_path, shape):
    """A vault with one source and the edge of ``shape``, a memory, an entity, and controls that name no deleted source.
    The import also wrote the mention edges that start at the source (``mentions``)."""

    db = _vault(tmp_path)
    folder = _folder(tmp_path, note=NOTE)
    other_folder = tmp_path / "other"
    other_folder.mkdir()
    (other_folder / "other.md").write_text("The second lantern is in the cellar.")
    sid = run_import(db, folder).source_ids[0]
    other = run_import(db, other_folder / "other.md").source_ids[0]
    kind, make = SHAPES[shape]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        chunk = str(store.list_source_chunks(sid)[0]["id"])
        memory = store.create_memory(
            {"memory_key": "kept", "value": {"text": "kept"}, "memory_type": "project_fact", "status": "active",
             "title": "kept", "canonical_text": "the lantern is kept", "source_event_ids": []}
        )
        mid = str(memory["id"])
        entity = _linker_entity(store, "Zorvex Holdings")
        fields = make(sid, chunk, mid, entity)
        what, where = NAMING.get(kind, (None, None))
        named = _edge(store, fields, shape, names_source={"source": sid, "chunk": chunk, None: None}[what],
                      names_in=where or "metadata")
        controls = {
            "between live rows": _edge(store, ("memory", mid, "entity", entity, "supports"), "control one",
                                       names_source=None),
            "names another source": _edge(store, ("source", other, "memory", mid, "supports"), "control two",
                                          names_source=other),
            "a prefix of the id": _edge(store, ("document", sid[:30], "memory", mid, "supports"), "control three"),
        }
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE graph_edges SET explanation = replace(explanation, ?, ?), metadata_json = replace(metadata_json, ?, ?) "
                     "WHERE id IN (%s)" % ",".join("?" * len(controls)),
                     (SENTINEL, KEPT, SENTINEL, KEPT, *controls.values()))
        conn.commit()
    mentions = [row[0] for row in _rows(db, "SELECT id FROM graph_edges WHERE from_id = ? AND edge_type = 'mentions' "
                                        "AND created_by = 'vnext_entity_linker'", (sid,))]
    assert mentions
    return Seed(db, folder, sid, mid, entity, named, controls, kind, mentions)


def _retire(db, folder, sid, operation, capsys):
    if operation == "delete":
        assert command(db, "delete", sid) == 2
        preview = json.loads(capsys.readouterr().out)["would_delete"][0]
        assert command(db, "delete", sid, "--yes") == 0
        receipt = json.loads(capsys.readouterr().out)["deleted"][0]
        return preview, receipt
    (folder / "note.md").write_text("The copper lantern is upstairs.")
    run_import(db, folder, supersede=True)
    if operation == "prune":
        assert command(db, "prune", "--superseded", "--yes") == 0
        return None, json.loads(capsys.readouterr().out)["deleted"][0]
    payload = _rows(db, "SELECT payload_json FROM event_log WHERE event_type = 'source.superseded' ORDER BY rowid DESC")
    return None, json.loads(payload[0][0])


@pytest.mark.parametrize("operation", ("delete", "prune", "replace"))
@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_shape_is_retired(tmp_path, capsys, shape, operation):
    seed = _seed(tmp_path, shape)
    db, folder, sid, mid, entity, named, controls, kind = (
        seed.db, seed.folder, seed.sid, seed.mid, seed.entity, seed.named, seed.controls, seed.kind)
    assert SENTINEL in _whole_database(db)
    preview, receipt = _retire(db, folder, sid, operation, capsys)
    # A prune follows the replacement that already blanked an edge naming the source only in its text.
    expected = len(seed.mentions) + (0 if operation == "prune" and kind.startswith("notes") else 1)
    assert receipt["graph_edges"] == expected, receipt
    assert receipt["mention_edges"] == len(seed.mentions)
    if preview is not None:
        assert preview["graph_edges"] == receipt["graph_edges"]
    row = _rows(db, "SELECT explanation, metadata_json, valid_to IS NULL FROM graph_edges WHERE id = ?", (named,))[0]
    assert row[:2] == ("", "{}"), (shape, row)
    if kind == "ends":
        assert row[2] == 0, (shape, "the edge with the source at an end is still open")
    else:
        assert row[2] == 1, (shape, "an edge that names the source only in its text keeps its link")
    assert SENTINEL not in _whole_database(db)
    assert SENTINEL not in _export_text(db)
    for label, edge_id in controls.items():
        control = _rows(db, "SELECT explanation, metadata_json, valid_to IS NULL FROM graph_edges WHERE id = ?", (edge_id,))[0]
        assert KEPT in control[0] and KEPT in control[1] and control[2] == 1, (label, control)
    # The keyless owner's legacy tool, which returned these edges after the delete.
    for target in {sid, mid, entity}:
        for edge in _neighbours(db, target):
            assert SENTINEL not in json.dumps(edge, default=str), (shape, target)
    # Both ends still read the live edges.
    assert any(KEPT in json.dumps(edge, default=str) for edge in _neighbours(db, mid))


def test_the_finder_names_exactly_the_edges_that_name_the_source(tmp_path):
    seed = _seed(tmp_path, "capitals")
    db, sid, mid, entity, named, controls = seed.db, seed.sid, seed.mid, seed.entity, seed.named, seed.controls
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        text_only = _edge(store, ("memory", mid, "entity", entity, "supports"), "text only", names_source=sid)
        chunk = str(store.list_source_chunks(sid)[0]["id"])
        by_chunk = _edge(store, ("source_chunk", chunk, "memory", mid, "supports"), "chunk")
        chunk_text = _edge(store, ("memory", mid, "entity", entity, "supports"), "chunk in text", names_source=chunk)
        found = edges_naming_sources(store, [sid])[sid]
        control_ids = set(controls.values())
    assert set(found.ends) == {named, by_chunk, *seed.mentions}
    assert set(found.notes) == {text_only, chunk_text}
    assert not control_ids & (set(found.ends) | set(found.notes))


def test_preview_and_receipt_count_the_same_edges(tmp_path, capsys):
    seed = _seed(tmp_path, "source to memory")
    db, folder, sid, mid, entity = seed.db, seed.folder, seed.sid, seed.mid, seed.entity
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        _edge(store, ("memory", mid, "source", sid.replace("-", ""), "supports"), "second")
        _edge(store, ("memory", mid, "entity", entity, "supports"), "text", names_source=sid)
    mentions = len(seed.mentions)
    preview, receipt = _retire(db, folder, sid, "delete", capsys)
    assert preview["graph_edges"] == receipt["graph_edges"] == mentions + 3
    assert receipt["mention_edges"] == mentions


def test_another_users_edge_is_left_alone(tmp_path, capsys):
    seed = _seed(tmp_path, "source to memory")
    db, folder, sid, named = seed.db, seed.folder, seed.sid, seed.named
    other = str(uuid4())
    neighbour = str(uuid4())
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO users (id, email) VALUES (?, ?)", (other, "other@alice"))
        conn.execute(
            "INSERT INTO graph_edges (id, user_id, from_type, from_id, to_type, to_id, edge_type, explanation, created_by) "
            "VALUES (?, ?, 'source', ?, 'memory', ?, 'supports', ?, 'test')",
            (neighbour, other, sid, str(uuid4()), f"{KEPT} neighbour"),
        )
        conn.commit()
    before = _rows(db, "SELECT * FROM graph_edges WHERE id = ?", (neighbour,))
    preview, receipt = _retire(db, folder, sid, "delete", capsys)
    assert _rows(db, "SELECT * FROM graph_edges WHERE id = ?", (neighbour,)) == before
    # The other user's edge names the source too, and is neither counted nor touched.
    assert preview["graph_edges"] == receipt["graph_edges"] == 1 + len(seed.mentions)
    assert _rows(db, "SELECT valid_to IS NULL FROM graph_edges WHERE id = ?", (named,)) == [(0,)]


def test_an_entity_held_only_by_a_non_mention_edge_goes_too(tmp_path, capsys):
    """The entity the linker made has one live edge left, and it is not a mention edge. Retiring that edge leaves the
    entity with none, so its name goes with it, as it does when the last edge is a mention edge."""

    seed = _seed(tmp_path, "source to entity, not a mention")
    db, folder, sid, entity, controls = seed.db, seed.folder, seed.sid, seed.entity, seed.controls
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DELETE FROM graph_edges WHERE id IN (%s)" % ",".join("?" * len(controls)), tuple(controls.values()))
        conn.commit()
    assert _rows(db, "SELECT name, deleted_at IS NULL FROM vnext_entities WHERE id = ?", (entity,)) == [("Zorvex Holdings", 1)]
    _retire(db, folder, sid, "delete", capsys)
    assert _rows(db, "SELECT name, deleted_at IS NULL FROM vnext_entities WHERE id = ?", (entity,)) == [(MARKER, 0)]
    # The entity the import's own mention edge made goes the same way.
    assert _rows(db, "SELECT count(*) FROM vnext_entities WHERE deleted_at IS NULL AND name = 'Bob Smith'") == [(0,)]


def test_an_entity_kept_by_a_live_edge_keeps_its_name(tmp_path, capsys):
    seed = _seed(tmp_path, "source to entity, not a mention")
    db, folder, sid, entity = seed.db, seed.folder, seed.sid, seed.entity
    # ``controls['between live rows']`` still ties the entity to the live memory.
    _retire(db, folder, sid, "delete", capsys)
    assert _rows(db, "SELECT name, deleted_at IS NULL FROM vnext_entities WHERE id = ?", (entity,)) == [("Zorvex Holdings", 1)]


def test_edges_at_a_scrubbed_candidate_go_with_it(tmp_path, capsys):
    """A candidate memory that cites the source is redacted by the scrub. An edge of any type that has it at an end carries
    text about it, so it is closed and blanked as well; the same edge at the live memory is left alone."""

    db = _vault(tmp_path)
    folder = _folder(tmp_path, note=NOTE)
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        candidate = store.create_memory(
            {"memory_key": "pending", "value": {"text": NOTE}, "canonical_text": NOTE, "title": NOTE, "status": "candidate",
             "memory_type": "semantic", "metadata_json": {"source_id": sid}, "source_event_ids": [sid]}
        )
        cid = str(candidate["id"])
        store.create_provenance_link({"target_type": "memory", "target_id": cid, "source_id": sid, "quote": NOTE})
        live = store.create_memory(
            {"memory_key": "live", "value": {"text": "live"}, "memory_type": "project_fact", "status": "active",
             "title": "live", "canonical_text": "live", "source_event_ids": []}
        )
        live_id = str(live["id"])
        entity = _linker_entity(store, "Zorvex Holdings")
        at_candidate = _edge(store, ("memory", cid, "entity", entity, "supports"), "at candidate")
        at_candidate_upper = _edge(store, ("entity", entity, "memory", cid.upper(), "supports"), "at candidate upper")
        at_live = _edge(store, ("memory", live_id, "entity", entity, "supports"), "at live")
    mentions = _rows(db, "SELECT count(*) FROM graph_edges WHERE from_id = ? AND edge_type = 'mentions'", (sid,))[0][0]
    assert command(db, "delete", sid) == 2
    preview = json.loads(capsys.readouterr().out)["would_delete"][0]
    assert command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    # The two edges at the redacted candidate are counted with the edges of the source: the preview says what the receipt
    # does, and both say what changed in the file.
    assert preview["graph_edges"] == receipt["graph_edges"] == mentions + 2, (preview, receipt)
    assert receipt["mention_edges"] == mentions and receipt["candidate_memories"] == 1
    for edge_id in (at_candidate, at_candidate_upper):
        assert _rows(db, "SELECT explanation, metadata_json, valid_to IS NULL FROM graph_edges WHERE id = ?", (edge_id,)) == [
            ("", "{}", 0)]
    live_row = _rows(db, "SELECT explanation, valid_to IS NULL FROM graph_edges WHERE id = ?", (at_live,))[0]
    assert SENTINEL in live_row[0] and live_row[1] == 1


@pytest.mark.parametrize("operation", ("delete", "prune", "replace"))
def test_edges_at_a_blanked_open_loop_go_with_it(tmp_path, capsys, operation):
    """An open loop that names the source is dismissed and its text is blanked. An edge that has it at an end carries text
    about it, so it is closed and blanked as well, in any spelling of the loop id; the same edge at a loop that names
    another source is left alone."""

    db = _vault(tmp_path)
    folder = _folder(tmp_path, note=NOTE)
    sid = run_import(db, folder).source_ids[0]
    other_folder = tmp_path / "other"
    other_folder.mkdir()
    (other_folder / "other.md").write_text("The second lantern is in the cellar.")
    other_source = run_import(db, other_folder / "other.md").source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        loop = str(store.create_open_loop({"title": "find the lantern", "description": "kestrelplum", "resolution_note": None,
                                           "source_id": sid, "status": "open", "metadata_json": {}})["id"])
        other = str(store.create_open_loop({"title": "other loop", "description": KEPT, "resolution_note": None,
                                            "source_id": other_source, "status": "open", "metadata_json": {}})["id"])
        memory = store.create_memory(
            {"memory_key": "live", "value": {"text": "live"}, "memory_type": "project_fact", "status": "active",
             "title": "live", "canonical_text": "live", "source_event_ids": []})
        mid = str(memory["id"])
        at_loop = _edge(store, ("open_loop", loop, "memory", mid, "supports"), "at loop")
        at_loop_upper = _edge(store, ("memory", mid, "open_loop", loop.upper(), "supports"), "at loop upper")
        at_other = _edge(store, ("open_loop", other, "memory", mid, "supports"), "at other loop")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE graph_edges SET explanation = replace(explanation, ?, ?), metadata_json = replace(metadata_json, ?, ?) "
                     "WHERE id = ?", (SENTINEL, KEPT, SENTINEL, KEPT, at_other))
        conn.commit()
    mentions = _rows(db, "SELECT count(*) FROM graph_edges WHERE from_id = ? AND edge_type = 'mentions'", (sid,))[0][0]
    preview, receipt = _retire(db, folder, sid, operation, capsys)
    assert receipt["open_loops"] == 1
    if operation != "prune":
        assert receipt["graph_edges"] == mentions + 2, receipt
        assert receipt["mention_edges"] == mentions
    if preview is not None:
        assert preview["graph_edges"] == receipt["graph_edges"]
    for edge_id in (at_loop, at_loop_upper):
        assert _rows(db, "SELECT explanation, metadata_json, valid_to IS NULL FROM graph_edges WHERE id = ?", (edge_id,)) == [
            ("", "{}", 0)], operation
    control = _rows(db, "SELECT explanation, valid_to IS NULL FROM graph_edges WHERE id = ?", (at_other,))[0]
    assert KEPT in control[0] and control[1] == 1
    assert SENTINEL not in _whole_database(db)


def test_a_prune_after_a_replacement_does_not_count_an_entity_mention_twice(tmp_path, capsys):
    """The replacement closes the edges and counts the mentions down. The prune meets the same edges closed, blanks them
    again and must not count them down a second time, even for an edge that names two of the pruned sources."""

    db = _vault(tmp_path)
    folder = tmp_path / "pair"
    folder.mkdir()
    (folder / "a.md").write_text("Bob Smith keeps the first amber lantern.")
    (folder / "b.md").write_text("Bob Smith keeps the second bronze lantern.")
    first = run_import(db, folder / "a.md").source_ids[0]
    second = run_import(db, folder / "b.md").source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        mid = str(store.create_memory({"memory_key": "m", "value": {"text": "m"}, "memory_type": "project_fact",
                                       "status": "active", "title": "m", "canonical_text": "m", "source_event_ids": []})["id"])
        shared = _edge(store, ("source", first, "memory", mid, "supports"), "shared", names_source=second)
        conn.execute("UPDATE vnext_entities SET metadata_json = json_set(metadata_json, '$.created_by', 'user')")
    (folder / "a.md").write_text("Bob Smith keeps the first copper lantern.")
    (folder / "b.md").write_text("Bob Smith keeps the second silver lantern.")
    run_import(db, folder, supersede=True)
    counted = _rows(db, "SELECT sum(mention_count) FROM vnext_entities")
    assert command(db, "prune", "--superseded", "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)
    assert {row["id"] for row in receipt["deleted"]} == {first, second}
    assert _rows(db, "SELECT sum(mention_count) FROM vnext_entities") == counted
    assert _rows(db, "SELECT explanation, metadata_json, valid_to IS NULL FROM graph_edges WHERE id = ?", (shared,)) == [
        ("", "{}", 0)]


def _count_edge_scans(monkeypatch):
    scans = []
    original = source_retirement._edge_candidates

    def counting(self, probes):
        scans.append(len(probes.ids))
        return original(self, probes)

    monkeypatch.setattr(source_retirement, "_edge_candidates", counting)
    return scans


def test_a_folder_import_reads_the_edges_once(tmp_path, monkeypatch):
    db = _vault(tmp_path)
    folder = tmp_path / "many"
    folder.mkdir()
    for name in ("a", "b", "c"):
        (folder / f"{name}.md").write_text(f"The {name} lantern before.")
    run_import(db, folder)
    scans = _count_edge_scans(monkeypatch)
    for name in ("a", "b", "c"):
        (folder / f"{name}.md").write_text(f"The {name} lantern after.")
    assert run_import(db, folder, supersede=True).to_record()["superseded_count"] == 3
    # One pass for the three replaced files; its probes are the three sources and their chunks.
    assert len(scans) == 1 and scans[0] >= 3, scans


def test_a_folder_that_changed_nothing_reads_no_edge(tmp_path, monkeypatch):
    """Re-importing a folder is the usual run and replaces nothing: it must not read the user's edges to find out."""

    db = _vault(tmp_path)
    folder = tmp_path / "many"
    folder.mkdir()
    for name in ("a", "b", "c"):
        (folder / f"{name}.md").write_text(f"The {name} lantern stays.")
    run_import(db, folder)
    scans = _count_edge_scans(monkeypatch)
    assert run_import(db, folder, supersede=True).to_record().get("superseded_count", 0) == 0
    assert scans == []
    (folder / "b.md").write_text("The b lantern moved.")
    assert run_import(db, folder, supersede=True).to_record()["superseded_count"] == 1
    assert len(scans) == 1, scans


def _spliced_restore(tmp_path, source_db, edge_rows):
    """A vault restored from the export of ``source_db`` with ``edge_rows`` added to the file, as a restore carries an edge
    that another vault wrote. The footer is computed again, as the exporter computes it."""

    exported = tmp_path / "exported.jsonl"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli(["export", "--db", str(source_db), "--user-id", USER_ID, "--out", str(exported)]) == 0
    lines = exported.read_text().splitlines()
    head, footer, records = lines[0], json.loads(lines[-1]), lines[1:-1]
    records.extend(onramp._export_line("graph_edge", row) for row in edge_rows)
    digest = hashlib.sha256()
    for record in records:
        digest.update((record + "\n").encode())
    body = footer["record"]
    body["record_count"] = len(records)
    body["record_counts"]["graph_edge"] = body["record_counts"].get("graph_edge", 0) + len(edge_rows)
    body["sha256"] = digest.hexdigest()
    spliced = tmp_path / "spliced.jsonl"
    spliced.write_text("\n".join([head, *records, onramp._export_line(onramp._EXPORT_FOOTER_TYPE, body)]) + "\n")
    target = tmp_path / "restored.sqlite3"
    onramp.bootstrap_database(target, user_id=USER_ID, user_email="local@alice")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli(["import", "--in", str(spliced), "--db", str(target), "--user-id", USER_ID]) == 0
    return target


@pytest.mark.parametrize("shape", ("source to memory", "another from type", "text only"))
def test_an_imported_edge_is_gone_from_the_legacy_tool_after_delete_export_and_import(tmp_path, capsys, shape):
    """The reported path: a restore brings in an edge that is not a mention edge and names the source. After the delete,
    the keyless owner's ``alice_graph_neighborhood`` must not return its text from this vault, from an export of it, or
    from a vault restored from that export."""

    kind, make = SHAPES[shape]
    origin = tmp_path / "origin"
    origin.mkdir()
    db = _vault(origin)
    folder = _folder(origin, note=NOTE)
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        mid = str(store.create_memory({"memory_key": "m", "value": {"text": "m"}, "memory_type": "project_fact",
                                       "status": "active", "title": "m", "canonical_text": "m", "source_event_ids": []})["id"])
        entity = str(store._fetch_one("entity", "SELECT id FROM vnext_entities LIMIT 1")["id"])
        chunk = str(store.list_source_chunks(sid)[0]["id"])
    from_type, from_id, to_type, to_id, edge_type = make(sid, chunk, mid, entity)
    edge_id = str(uuid4())
    row = {
        "id": edge_id, "user_id": USER_ID, "from_type": from_type, "from_id": from_id, "to_type": to_type, "to_id": to_id,
        "edge_type": edge_type, "confidence": 0.9, "explanation": f"{SENTINEL} explanation", "created_by": "vnext_connection_finder",
        "created_at": "2030-01-01T00:00:00Z", "observed_at": "2030-01-01T00:00:00Z", "valid_from": "2030-01-01T00:00:00Z",
        "valid_to": None, "metadata_json": {"connection": {"source_item": f"source:{sid}"}, "why": SENTINEL},
    }
    restored = _spliced_restore(tmp_path, db, [row])
    assert _rows(restored, "SELECT count(*) FROM graph_edges WHERE id = ?", (edge_id,)) == [(1,)]
    assert any(SENTINEL in json.dumps(edge) for target in (sid, mid, entity) for edge in _neighbours(restored, target))
    assert command(restored, "delete", sid, "--yes") == 0
    capsys.readouterr()
    for target in (sid, mid, entity, from_id, to_id):
        for edge in _neighbours(restored, str(target)):
            assert SENTINEL not in json.dumps(edge), (shape, target)
    assert SENTINEL not in _whole_database(restored)
    exported = _export_text(restored)
    assert SENTINEL not in exported
    again = tmp_path / "again"
    again.mkdir()
    second = _spliced_restore(again, restored, [])
    assert SENTINEL not in _whole_database(second)
    for target in (sid, mid, entity):
        for edge in _neighbours(second, target):
            assert SENTINEL not in json.dumps(edge)


def test_the_freed_pages_of_a_vacuumed_copy_hold_no_edge_text(tmp_path, capsys):
    seed = _seed(tmp_path, "source to memory")
    db, sid = seed.db, seed.sid
    assert command(db, "delete", sid, "--yes") == 0
    capsys.readouterr()
    copy = tmp_path / "vacuumed.db"
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(copy)) as target:
        source.backup(target)
        target.execute("VACUUM")
    assert SENTINEL.encode() not in copy.read_bytes().lower()
    assert KEPT.encode() in copy.read_bytes().lower()


def test_no_core_tool_returns_an_edge_for_any_caller_and_the_legacy_ones_need_no_key(tmp_path, monkeypatch):
    """Whether a key with limits could reach an edge that outlives its source. The tools that return edge rows
    (``alice_graph_neighborhood``, ``alice_graph_edge_review``) are legacy tools, and a legacy tool is refused whenever
    ``ALICE_AGENT_API_KEY`` is configured. No core tool returns an edge row, for the owner or for a caller with limits,
    and ``alice_explain`` for an entity is refused on SQLite."""

    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    seed = _seed(tmp_path, "source to memory")
    db, mid, entity, named, controls = seed.db, seed.mid, seed.entity, seed.named, seed.controls
    edge_ids = [named, *controls.values()]
    callers = {
        "owner": None,
        "read only": AgentIdentity(agent_id="r", permission_profile="read_only_agent", auth="agent_api_key"),
        "project bound": AgentIdentity(agent_id="p", permission_profile="project_scoped_agent", auth="agent_api_key",
                                       project_scope=("Alice",), project_scope_locked=True),
        "trusted": AgentIdentity(agent_id="t", permission_profile="trusted_local_agent", auth="agent_api_key"),
        "admin": AgentIdentity(agent_id="a", permission_profile="admin_agent", auth="agent_api_key"),
    }
    calls = [
        ("alice_recall", {"query": "kestrelplum lantern Bob Smith", "include_sources": True}),
        ("alice_context_pack", {"query": "kestrelplum lantern", "include_sources": True, "include_contradictions": True}),
        ("alice_resume", {"query": "lantern"}),
        ("alice_recent_decisions", {}),
        ("alice_memory_review", {"status": "all"}),
        ("alice_memory_review", {"review_item_id": mid}),
        ("alice_open_loops", {"action": "list"}),
        ("alice_explain", {"memory_id": mid}),
    ]
    for name, arguments in calls:
        for label, identity in callers.items():
            keyword = {} if identity is None else {"agent_identity": identity, "agent_identity_resolved": True}
            context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID), **keyword)
            try:
                text = json.dumps(call_mcp_tool(context, name=name, arguments=dict(arguments)), default=str)
            except Exception as exc:  # a refusal is not a leak
                assert type(exc).__name__.startswith("MCP"), (name, label, exc)
                continue
            assert SENTINEL not in text and not any(edge_id in text for edge_id in edge_ids), (name, label)
    for label, identity in callers.items():
        keyword = {} if identity is None else {"agent_identity": identity, "agent_identity_resolved": True}
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID), **keyword)
        with pytest.raises(Exception) as refused:
            call_mcp_tool(context, name="alice_explain", arguments={"entity_id": entity})
        assert "Postgres" in str(refused.value), label
    # The keyless owner reads the edge through the legacy tool, and a configured key takes the tool away.
    assert any(SENTINEL in json.dumps(edge) for edge in _neighbours(db, mid))
    monkeypatch.setenv("ALICE_AGENT_API_KEY", "a-key-that-is-only-configured")
    for name, arguments in (
        ("alice_graph_neighborhood", {"target_id": mid}),
        ("alice_graph_edge_review", {"edge_id": named, "action": "review"}),
    ):
        with pytest.raises(Exception) as refused:
            _tool(db, name, arguments)
        assert "legacy MCP surface" in str(refused.value), name


def test_relationship_events_cannot_be_scrubbed_and_no_product_writer_names_a_source():
    """``entity_relationship_events.source_id`` is the one column that can name a source and that retirement leaves alone.
    The table is append-only (an UPDATE or a DELETE aborts, in redaction mode too), and nothing in the product calls
    ``record_relationship_change``, so the column is never filled. A new caller must come with a decision about it."""

    import pathlib
    import re

    source_root = pathlib.Path(source_retirement.__file__).resolve().parents[2]
    calls = []
    for path in source_root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.record_relationship_change\(", line):
                calls.append(f"{path.relative_to(source_root)}:{number}")
    assert calls == []


def test_relationship_events_are_append_only(tmp_path):
    db = _vault(tmp_path)
    entity_id = str(uuid4())
    event_id = str(uuid4())
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO vnext_entities (id, user_id, entity_type, name, normalized_name) VALUES (?, ?, 'other', 'n', 'n')",
                     (entity_id, USER_ID))
        conn.execute("INSERT INTO entity_relationship_events (id, user_id, entity_id, relationship_type_after, source_id) "
                     "VALUES (?, ?, ?, 'friend', ?)", (event_id, USER_ID, entity_id, str(uuid4())))
        conn.commit()
        for statement in ("UPDATE entity_relationship_events SET source_id = NULL", "DELETE FROM entity_relationship_events"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
        conn.execute("INSERT INTO redaction_mode (id, enabled) VALUES (1, 1) ON CONFLICT(id) DO UPDATE SET enabled = 1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE entity_relationship_events SET source_id = NULL")


KEYWORD = "kestrelplum"


def test_everything_that_links_to_or_copies_a_source_is_covered_or_listed_as_kept(tmp_path, capsys):
    """The census of what can link to or copy from a source on SQLite, one assertion for each kind, and the kinds that stay.

    Retired with the source: the source row, its chunks and their full-text rows, the quotes of its provenance links,
    the candidate memory it produced with its revision, full-text row and edges, its open loops, its mention edges and
    the entities only they held, every other graph edge that names it, and its sleep proposal. Kept on purpose, and
    listed in the receipt or in ``retained_data``: a committed memory that cites it, the events that name it, and the
    hash columns of the source row. Not a SQLite table at all: generated reports (artifacts), connector cursors, and a
    separate embedding table (the only embedding column is on a memory). ``entity_relationship_events`` is append-only,
    and nothing writes its ``source_id`` (see the two tests at the end of this file).
    """

    from tests.unit.test_source_commands import TEXT, seed

    db = _vault(tmp_path)
    folder = _folder(tmp_path, note=TEXT)
    sid, candidate = seed(db, folder)
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        committed = str(
            store.create_memory(
                {"memory_key": "kept.committed", "value": {"text": TEXT}, "canonical_text": TEXT, "title": "kept", "status": "active",
                 "memory_type": "semantic", "metadata_json": {"source_id": sid}, "source_event_ids": [sid]}
            )["id"]
        )
        store.create_provenance_link({"target_type": "memory", "target_id": committed, "source_id": sid, "quote": TEXT})
        entity = _linker_entity(store, "Zorvex Holdings")
        edges = [
            _edge(store, ("source", sid, "memory", committed, "supports"), "to a committed memory"),
            _edge(store, ("entity", entity, "source", sid.upper(), "supports"), "from an entity"),
            _edge(store, ("memory", candidate, "entity", entity, "supports"), "at the candidate"),
        ]
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE memories SET embedding = ? WHERE id IN (?, ?)", (b"\x01\x02\x03\x04", candidate, committed))
        conn.commit()
    assert command(db, "delete", sid, "--yes") == 0
    receipt = json.loads(capsys.readouterr().out)["deleted"][0]
    assert receipt["candidate_memories"] == 1 and receipt["open_loops"] == 1
    # The only vector column belongs to a memory: the redacted candidate loses it, the kept memory keeps its own.
    assert _rows(db, "SELECT id, embedding IS NULL FROM memories WHERE id IN (?, ?) ORDER BY id", (candidate, committed)) == sorted(
        [(candidate, 1), (committed, 0)])
    assert receipt["memories_citing_replaced"] == [committed]
    assert receipt["graph_edges"] >= 2
    holding = set()
    with closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            try:
                rows = list(conn.execute(f'SELECT * FROM "{name}"'))
            except sqlite3.DatabaseError:
                continue
            if any(KEYWORD in json.dumps(list(row), default=str).casefold() for row in rows):
                holding.add(name)
    # The events keep the title and the paths, and the committed memory keeps its own text, in its row and in the
    # full-text index of memories. Nothing else holds a word.
    assert holding == {"event_log", "memories", "memories_fts"}, holding
    kept_memories = [row[0] for row in _rows(db, "SELECT id FROM memories WHERE lower(value) LIKE ? OR lower(canonical_text) LIKE ?",
                                              (f"%{KEYWORD}%", f"%{KEYWORD}%"))]
    assert kept_memories == [committed]
    for edge_id in edges:
        assert SENTINEL not in json.dumps(_rows(db, "SELECT * FROM graph_edges WHERE id = ?", (edge_id,)))
    assert _rows(db, "SELECT count(*) FROM source_chunks_fts WHERE source_chunks_fts MATCH ?", (KEYWORD,)) == [(0,)]
    assert _rows(db, "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", (KEYWORD,)) == [
        (row[0],) for row in _rows(db, "SELECT rowid FROM memories WHERE id = ?", (committed,))]
    assert not [r for r in _rows(db, "SELECT name FROM vnext_entities WHERE deleted_at IS NULL") if "Bob" in r[0]]
    # No table for reports, connector cursors or vectors; ``embedding_stamp`` holds one token and no vector.
    assert [r[0] for r in _rows(db, "SELECT name FROM sqlite_master WHERE type = 'table' AND (name LIKE '%artifact%' "
                                    "OR name LIKE '%cursor%' OR name LIKE '%embedding%')")] == ["embedding_stamp"]


def test_an_alias_a_shared_entity_learned_from_the_source_stays_while_another_edge_reaches_it(tmp_path, capsys) -> None:
    """Kept, and listed as kept in the importers page and the changelog. An entity is shared by every source and memory
    that names it, and nothing records which source taught it an honorific alias. The alias goes with the entity when no
    live edge reaches the entity any more (``test_an_entity_held_only_by_a_non_mention_edge_goes_too`` and the census
    above). This test pins the limit, so a change to it is a decision and not an accident."""

    db = _vault(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.md").write_text("Alice Marlow holds the amber lantern.\n")
    (second / "b.md").write_text("Dr Alice Marlow keeps the copper key.\n")
    run_import(db, first)
    sid = run_import(db, second).source_ids[0]
    assert _rows(db, "SELECT name, aliases FROM vnext_entities") == [("Alice Marlow", '["dr alice marlow"]')]
    assert command(db, "delete", sid, "--yes") == 0
    capsys.readouterr()
    assert _rows(db, "SELECT name, aliases, mention_count, deleted_at IS NULL FROM vnext_entities") == [
        ("Alice Marlow", '["dr alice marlow"]', 1, 1)]
