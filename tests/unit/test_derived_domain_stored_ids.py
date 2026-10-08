"""The SQLite derived-label repair updates a row under the id the row is stored with.

``plan_relabels`` matches rows and their recorded inputs by a normalised id (``_identifier``: lower case, hyphenated).
SQLite compares ids as text, so a derived memory stored as ``F9D0...`` (capitals), compact, braced or ``urn:uuid:``
is not the row ``id = 'f9d0...'``. The repair once ran ``UPDATE ... WHERE id = <normalised>`` anyway, changed no row, and
still wrote the relabel event and the completion stamp, so the row kept its unrestricted label, the stamp said it had
been repaired, and the pair survived export and import. ``relabel_sqlite`` now updates and records each row under its
stored id, uses the normalised id only to match the graph, and refuses with ``DerivedDomainRepairError`` when an update
changes no row, before it writes an event or the stamp.

Mutations, each alone, each named by the test that fails:

* update by the normalised id (``stored_id`` back to ``row_id`` in the ``UPDATE`` call):
  ``test_open_relabels_a_row_stored_under_another_spelling`` and the restore case fail.
* record the event under the normalised id: the open case fails on the event target, and
  ``test_the_same_backup_restores_again_into_the_repaired_vault`` fails on the second restore.
* drop the zero-row refusal: ``test_a_zero_row_update_refuses_without_an_event_or_stamp`` (open and restore) fails.
* write the events before every update has been checked: the same test fails on the event count.
* plan from one stored row per normalised id, the last one (``plan_stored_relabels`` once left the earlier spelling at
  its old label when the last row already held the planned label): ``test_every_spelling_of_one_id_is_relabelled`` and
  ``test_the_earlier_spelling_is_relabelled_when_the_last_row_already_holds_the_planned_label`` fail. The first one:
  ``test_every_spelling_of_one_id_is_relabelled`` fails.
* record a spelling that already holds the planned label as a change: ``test_a_spelling_already_at_the_planned_label_is_left_alone``.
* compare each spelling with the starting label of the id and not the label its own inputs give it:
  ``test_no_spelling_changes_label_when_the_inputs_name_no_restricted_label`` fails (two restricted labels on two rows).
* ``plan_stored_relabels`` reads an input's raw label: ``test_for_rows_with_unique_ids_the_stored_plan_is_the_plan`` fails.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

import pytest

from alicebot_api import sqlite_schema, vnext_derived_domain_backfill as repair
from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_writes import without_insert_floor
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_derived_domain_fence import USER

SPELLINGS = {
    "upper": lambda text: text.upper(),
    "compact": lambda text: text.replace("-", ""),
    "compact_upper": lambda text: text.replace("-", "").upper(),
    "braced": lambda text: "{" + text + "}",
    "urn": lambda text: "urn:uuid:" + text,
}


def _stored(spelling: str, canonical: str) -> str:
    stored = SPELLINGS[spelling](canonical)
    assert stored != canonical and str(UUID(stored)) == canonical
    return stored


def _old_vault(path, monkeypatch, spellings=("upper",)):
    """A vault from before the repair: a health memory and, per spelling, a derived memory that records it as input,
    stored under a spelling of its id that is not the canonical one. Returns the stored ids."""

    stored_ids = []
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        bootstrap_database(path, user_id=USER, user_email="local@alice")
        with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            health = add_memory(store, key="health", text="A restricted observation", domain="health")
            canonical_ids = []
            for index, _ in enumerate(spellings):
                derived = store.create_memory(
                    {
                        "memory_key": f"derived-{index}",
                        "canonical_text": f"A restricted summary {index}",
                        "status": "active",
                        "domain": "unknown",
                        "sensitivity": "public",
                        "metadata_json": {"consolidation": {"cluster_member_ids": [health["id"]]}},
                    }
                )
                canonical_ids.append(str(derived["id"]))
        raw = sqlite3.connect(path, isolation_level=None)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            for spelling, canonical in zip(spellings, canonical_ids):
                stored = _stored(spelling, canonical)
                for table, column in (("memories", "id"), ("memory_revisions", "memory_id")):
                    raw.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (stored, canonical))
                stored_ids.append(stored)
            raw.execute("DELETE FROM alice_schema_state WHERE key LIKE 'derived_restricted_domains_%'")
        finally:
            raw.close()
    return stored_ids


def _rows(path):
    with sqlite3.connect(path) as raw:
        domains = dict(raw.execute("SELECT id, domain FROM memories WHERE memory_key LIKE 'derived-%'"))
        events = raw.execute(
            "SELECT target_id, payload_json FROM event_log WHERE event_type = 'memory.domain_relabelled' ORDER BY target_id"
        ).fetchall()
        stamps = raw.execute(
            "SELECT count(*) FROM alice_schema_state WHERE key = ?", (repair.REPAIR_STATE_KEY,)
        ).fetchone()[0]
    return domains, events, stamps


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_open_relabels_a_row_stored_under_another_spelling(tmp_path, monkeypatch, spelling):
    path = tmp_path / "vault.sqlite3"
    (stored,) = _old_vault(path, monkeypatch, (spelling,))
    assert _rows(path)[0] == {stored: "unknown"}
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    domains, events, stamps = _rows(path)
    assert domains == {stored: "health"}
    assert [(target, json.loads(payload)["domain"]) for target, payload in events] == [(stored, "health")]
    assert stamps == 1
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    assert _rows(path) == (domains, events, 1)


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_restore_relabels_a_row_stored_under_another_spelling(tmp_path, monkeypatch, spelling):
    source = tmp_path / "old.sqlite3"
    backup = tmp_path / "backup.jsonl"
    destination = tmp_path / "restored.sqlite3"
    (stored,) = _old_vault(source, monkeypatch, (spelling,))
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    assert onramp_main(["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]) == 0
    # Read without bootstrapping: the staged copy must already be repaired when it is published.
    domains, events, stamps = _rows(destination)
    assert domains == {stored: "health"}
    assert [target for target, _ in events] == [stored]
    assert stamps == 1


def test_the_same_backup_restores_again_into_the_repaired_vault(tmp_path, monkeypatch):
    """The restore compares an existing row with the file's row through the recorded repairs, by id as written."""

    source = tmp_path / "old.sqlite3"
    backup = tmp_path / "backup.jsonl"
    destination = tmp_path / "restored.sqlite3"
    _old_vault(source, monkeypatch, ("upper", "compact"))
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    argv = ["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]
    assert onramp_main(argv) == 0
    before = _rows(destination)
    assert onramp_main([*argv, "--mode", "skip"]) == 0
    assert _rows(destination) == before


def _twin_vault(path, monkeypatch, *, first_domain, last_domain="unknown", input_domain="health"):
    """Two derived memories that record one input (a ``health`` one unless ``input_domain`` says otherwise), stored under
    two spellings of one id: the first row (earlier in the table) under the upper case spelling with ``first_domain``,
    the second, the last row of the table, under the canonical one with ``last_domain``. The planner reads the last
    row of the two. Returns ``(upper, canonical)``."""

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        bootstrap_database(path, user_id=USER, user_email="local@alice")
        with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            health = add_memory(store, key="health", text="A restricted observation", domain=input_domain)
            metadata = {"consolidation": {"cluster_member_ids": [health["id"]]}}
            first, second = (
                str(
                    store.create_memory(
                        {"memory_key": f"derived-{index}", "canonical_text": f"Summary {index}", "status": "active",
                         "domain": "unknown", "sensitivity": "public", "metadata_json": metadata}
                    )["id"]
                )
                for index in range(2)
            )
        raw = sqlite3.connect(path, isolation_level=None)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            for table, column in (("memories", "id"), ("memory_revisions", "memory_id")):
                raw.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (second.upper(), first))
            raw.execute("UPDATE memories SET domain = ? WHERE id = ?", (first_domain, second.upper()))
            raw.execute("UPDATE memories SET domain = ? WHERE id = ?", (last_domain, second))
            raw.execute("DELETE FROM alice_schema_state WHERE key LIKE 'derived_restricted_domains_%'")
        finally:
            raw.close()
    return second.upper(), second


def test_every_spelling_of_one_id_is_relabelled(tmp_path, monkeypatch):
    """Two stored spellings of one id are one row to the planner, so the planned label goes to both, each recorded under
    its own stored id."""

    path = tmp_path / "vault.sqlite3"
    upper, canonical = _twin_vault(path, monkeypatch, first_domain="unknown")
    assert _rows(path)[0] == {upper: "unknown", canonical: "unknown"}
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    domains, events, _ = _rows(path)
    assert domains == {upper: "health", canonical: "health"}
    assert [target for target, _ in events] == sorted([upper, canonical])


def test_a_spelling_already_at_the_planned_label_is_left_alone(tmp_path, monkeypatch):
    """The spelling that already holds the planned label is neither updated nor recorded as a change."""

    path = tmp_path / "vault.sqlite3"
    upper, canonical = _twin_vault(path, monkeypatch, first_domain="health")
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    domains, events, _ = _rows(path)
    assert domains == {upper: "health", canonical: "health"}
    assert [(target, json.loads(payload)["previous_domain"]) for target, payload in events] == [(canonical, "unknown")]


def test_the_earlier_spelling_is_relabelled_when_the_last_row_already_holds_the_planned_label(tmp_path, monkeypatch):
    """The planner reads the last row of two spellings of one id. When that row already holds the planned label, the plan
    changes nothing for the id, and the earlier spelling would stay at its old label while the stamp says the vault is
    repaired. Every stored spelling is compared with the label the recorded inputs give it.

    Mutation: plan from the last row only (``plan_relabels`` and its stored ids, as before): this test fails on the
    label of the upper case row.
    """

    path = tmp_path / "vault.sqlite3"
    upper, canonical = _twin_vault(path, monkeypatch, first_domain="unknown", last_domain="health")
    assert _rows(path)[0] == {upper: "unknown", canonical: "health"}
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    domains, events, stamps = _rows(path)
    assert domains == {upper: "health", canonical: "health"}
    assert [(target, json.loads(payload)["previous_domain"]) for target, payload in events] == [(upper, "unknown")]
    assert stamps == 1


@pytest.mark.parametrize(("first", "last"), (("health", "unknown"), ("health", "legal"), ("legal", "health")))
def test_no_spelling_changes_label_when_the_inputs_name_no_restricted_label(tmp_path, monkeypatch, first, last):
    """The repair raises a label to the one its recorded inputs give and never moves a row for any other reason. Two
    spellings of one id that hold different labels, with an input that is not restricted, keep both labels and write no
    event, so a comparison with the label of the last row alone cannot lower a ``health`` row to ``unknown`` or move it
    to the other row's restricted label.

    Mutation: compare every stored spelling with the planner's starting label of the id (``labels[key]``) instead of the
    label its own inputs give it.
    """

    path = tmp_path / "vault.sqlite3"
    upper, canonical = _twin_vault(path, monkeypatch, first_domain=first, last_domain=last, input_domain="unknown")
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    domains, events, stamps = _rows(path)
    assert domains == {upper: first, canonical: last}
    assert events == [] and stamps == 1


def _parity_tables():
    from tests.unit import test_derived_domain_review as review

    settling_cycle = {
        "sources": [{"id": "health", "user_id": "u", "domain": "health"}],
        "generated_artifacts": [
            {"id": "a", "user_id": "u", "domain": "unknown", "metadata_json": {"input_summary": {"artifact_ids": ["b"]}}},
            {"id": "b", "user_id": "u", "domain": "unknown",
             "metadata_json": {"input_summary": {"artifact_ids": ["a"], "source_ids": ["health"]}}},
        ],
    }
    promoted = {
        "sources": [{"id": "source", "user_id": "u", "domain": "health"}],
        "generated_artifacts": [
            {"id": "report", "user_id": "u", "domain": "unknown",
             "metadata_json": {"input_summary": {"source_ids": ["source"]}}},
        ],
        "memories": [
            {"id": "promoted", "user_id": "u", "domain": "unknown",
             "value": {"kind": "promoted_artifact", "artifact_id": "report"}},
        ],
    }
    return [settling_cycle, review._chain(8), promoted]


def test_for_rows_with_unique_ids_the_stored_plan_is_the_plan():
    """``plan_stored_relabels`` (the SQLite repair) and ``plan_relabels`` (the PostgreSQL migration) change the same rows
    to the same labels when no id has two spellings, whatever the shape of the graph: a cycle that settles, a long chain
    and a promoted copy.

    Mutation: read an input's label from the raw column in ``plan_stored_relabels`` instead of its settled label.
    """

    for tables in _parity_tables():
        planned = repair.plan_relabels(tables)
        assert planned
        assert [(table, user, row_id, domain) for table, user, row_id, _, domain in repair.plan_stored_relabels(tables)] == planned


def test_a_zero_row_update_refuses_without_an_event_or_stamp(tmp_path, monkeypatch):
    """An update that changes no row stops the repair: on open every time, and on restore before anything is published."""

    from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError

    source = tmp_path / "old.sqlite3"
    backup = tmp_path / "backup.jsonl"
    (stored,) = _old_vault(source, monkeypatch, ("upper",))
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    # The statement of the original defect: it names a row that is not stored.
    monkeypatch.setitem(
        repair._SQLITE_UPDATES, "memories", "UPDATE memories SET domain = ? WHERE user_id = ? AND id = ? AND 0"
    )
    before = _rows(source)
    for _ in range(2):
        with pytest.raises(DerivedDomainRepairError, match="changed no row") as caught:
            bootstrap_database(source, user_id=USER, user_email="local@alice")
        assert stored in str(caught.value)
        assert _rows(source) == before
    destination = tmp_path / "restored.sqlite3"
    assert onramp_main(["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]) == 1
    assert not destination.exists() and sorted(item.name for item in tmp_path.iterdir()) == [
        "backup.jsonl",
        "old.sqlite3",
    ]


def test_a_refusal_writes_no_event_and_no_stamp_even_after_an_earlier_update(tmp_path, monkeypatch):
    """The first of two updates changes its row and the second changes none: the refusal comes before any event or the
    stamp, on the connection itself, not only after the caller rolls back."""

    from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError

    path = tmp_path / "vault.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        bootstrap_database(path, user_id=USER, user_email="local@alice")
        with without_insert_floor(), sqlite_user_connection(path, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            health = add_memory(store, key="health", text="A restricted observation", domain="health")
            ids = sorted(
                str(
                    store.create_memory(
                        {"memory_key": f"derived-{index}", "canonical_text": f"Summary {index}", "status": "active",
                         "domain": "unknown", "sensitivity": "public",
                         "metadata_json": {"consolidation": {"cluster_member_ids": [health["id"]]}}}
                    )["id"]
                )
                for index in range(2)
            )
        raw = sqlite3.connect(path, isolation_level=None)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            # The plan visits ids in order, so the row it reaches second is the one stored under capitals.
            for table, column in (("memories", "id"), ("memory_revisions", "memory_id")):
                raw.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (ids[1].upper(), ids[1]))
            raw.execute("DELETE FROM alice_schema_state WHERE key LIKE 'derived_restricted_domains_%'")
        finally:
            raw.close()
    # An update that finds only rows stored as lower case text: the second row, stored under capitals, is not found.
    monkeypatch.setitem(
        repair._SQLITE_UPDATES, "memories", "UPDATE memories SET domain = ? WHERE user_id = ? AND id = ? AND id = lower(id)"
    )
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(DerivedDomainRepairError, match="changed no row") as caught:
            repair.relabel_sqlite(conn)
        assert ids[1].upper() in str(caught.value)
        assert conn.execute("SELECT count(*) FROM event_log WHERE event_type = 'memory.domain_relabelled'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM alice_schema_state WHERE key = ?", (repair.REPAIR_STATE_KEY,)
        ).fetchone()[0] == 0
        # The first update did change its row inside the open transaction; the caller's rollback takes it back.
        assert conn.execute("SELECT domain FROM memories WHERE id = ?", (ids[0],)).fetchone()[0] == "health"
        conn.rollback()
        assert dict(conn.execute("SELECT id, domain FROM memories WHERE memory_key LIKE 'derived-%'")) == {
            ids[0]: "unknown",
            ids[1].upper(): "unknown",
        }
    finally:
        conn.close()
