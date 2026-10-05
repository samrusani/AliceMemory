"""SQLite insert floor and metadata merge. Scratch vaults only."""

from __future__ import annotations

from pathlib import Path

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_writes import merge_protected_metadata, without_insert_floor
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_derived_domain_fence import USER

ALPHA = "prj_" + "a" * 16


def _vault(path: Path):
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    return sqlite_user_connection(path, USER)


def test_a_copy_stored_after_its_source_is_floored(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Visit",
                "content_hash": "hash-floor",
                "domain": "health",
                "sensitivity": "confidential",
                "metadata_json": {"project_scope": [ALPHA]},
            }
        )
        memory = store.create_memory(
            {
                "memory_key": "extracted",
                "canonical_text": "A fact from the visit.",
                "status": "active",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {
                    "source_id": source["id"],
                    "extraction_rule": "sentence",
                    "project_scope": [ALPHA, "prj_" + "b" * 16],
                },
            }
        )
    assert memory["domain"] == "health"
    assert memory["sensitivity"] == "confidential"
    assert memory["metadata_json"]["project_scope"] == [ALPHA]
    assert memory["metadata_json"]["project_floor"] == [ALPHA]


def test_without_insert_floor_keeps_the_requested_label(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        health = add_memory(store, key="health", text="A restricted observation", domain="health")
        with without_insert_floor():
            derived = store.create_memory(
                {
                    "memory_key": "derived",
                    "canonical_text": "A summary",
                    "status": "active",
                    "domain": "unknown",
                    "sensitivity": "public",
                    "metadata_json": {"consolidation": {"cluster_member_ids": [health["id"]]}},
                }
            )
    assert derived["domain"] == "unknown"
    assert derived["sensitivity"] == "public"


def test_an_update_that_omits_a_marker_keeps_it(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        health = add_memory(store, key="health", text="A restricted observation", domain="health", scope=(ALPHA,))
        with without_insert_floor():
            derived = store.create_memory(
                {
                    "memory_key": "derived",
                    "canonical_text": "A summary",
                    "status": "active",
                    "domain": "health",
                    "sensitivity": "public",
                    "metadata_json": {
                        "consolidation": {"cluster_member_ids": [health["id"]]},
                        "project_scope": [ALPHA],
                        "project_floor": [ALPHA],
                        "note": "keep",
                    },
                }
            )
        stored = store.update_memory(
            memory_id=str(derived["id"]),
            patch={"metadata_json": {"note": "changed", "project_scope": ["other"]}},
        )
    assert stored["metadata_json"]["consolidation"]["cluster_member_ids"] == [health["id"]]
    assert stored["metadata_json"]["project_scope"] == [ALPHA]
    assert stored["metadata_json"]["project_floor"] == [ALPHA]
    assert stored["metadata_json"]["note"] == "changed"


def test_a_source_relabel_reaches_the_extracted_memory(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Visit",
                "content_hash": "hash-propagate",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {"project_scope": [ALPHA]},
            }
        )
        memory = store.create_memory(
            {
                "memory_key": "extracted",
                "canonical_text": "A fact from the visit.",
                "status": "active",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {"source_id": str(source["id"]), "project_scope": [ALPHA]},
            }
        )
        store.update_source(
            source_id=str(source["id"]),
            patch={"domain": "health", "sensitivity": "confidential"},
            actor_type="user",
        )
        stored = store.get_memory(str(memory["id"]))
    assert stored is not None
    assert stored["domain"] == "health"
    assert stored["sensitivity"] == "confidential"


def test_a_candidate_loop_is_floored_from_its_source(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Tasks",
                "content_hash": "hash-loop",
                "domain": "health",
                "sensitivity": "private",
                "metadata_json": {"project_scope": [ALPHA]},
            }
        )
        loop = store.create_open_loop(
            {
                "title": "Call the clinic",
                "source_id": str(source["id"]),
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {
                    "discovered_by": "vnext_daily_brief",
                    "source_id": str(source["id"]),
                    "project_scope": [ALPHA],
                },
            }
        )
    assert loop["domain"] == "health"
    assert loop["sensitivity"] == "private"


def test_supersede_raises_a_citing_memory(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    with _vault(path) as conn:
        store = SQLiteVNextStore(conn, USER)
        old = store.create_source(
            {
                "source_type": "markdown",
                "title": "Note",
                "content_hash": "hash-old",
                "raw_path": "notes/one.md",
                "connector_name": "markdown_folder",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {"relative_path": "notes/one.md"},
            }
        )
        new = store.create_source(
            {
                "source_type": "markdown",
                "title": "Note",
                "content_hash": "hash-new",
                "raw_path": "notes/one.md",
                "connector_name": "markdown_folder",
                "domain": "health",
                "sensitivity": "confidential",
                "metadata_json": {"relative_path": "notes/one.md"},
            }
        )
        memory = store.create_memory(
            {
                "memory_key": "cited",
                "canonical_text": "A fact.",
                "status": "active",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {"source_id": str(old["id"])},
            }
        )
        store.supersede_source(str(old["id"]), superseded_by=str(new["id"]))
        stored = store.get_memory(str(memory["id"]))
    assert stored is not None
    assert stored["domain"] == "health"
    assert stored["sensitivity"] == "confidential"


def test_merge_protected_metadata_keeps_label_keys_unless_the_write_is_a_relabel() -> None:
    stored = {
        "consolidation": {"cluster_member_ids": ["m"]},
        "project_scope": [ALPHA],
        "project_floor": [ALPHA],
        "note": "old",
    }
    patch = {"note": "new", "project_scope": ["other"]}
    merged = merge_protected_metadata(stored, patch, label_write=False)
    assert merged["project_scope"] == [ALPHA]
    assert merged["project_floor"] == [ALPHA]
    assert merged["consolidation"] == {"cluster_member_ids": ["m"]}
    assert merged["note"] == "new"
    relabel = merge_protected_metadata(stored, {"project_scope": [], "project_floor": [ALPHA]}, label_write=True)
    assert relabel["project_scope"] == []
    assert relabel["consolidation"] == {"cluster_member_ids": ["m"]}


def test_insert_floor_survives_two_hops(tmp_path: Path):
    db = tmp_path / 'labels.sqlite3'
    bootstrap_database(db, user_id=USER, user_email='synthetic@example.test')
    with sqlite_user_connection(db, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({'source_type': 'note', 'title': 'Synthetic restricted note', 'content_hash': 'review-two-hops', 'domain': 'health', 'sensitivity': 'confidential', 'metadata_json': {'project_scope': [ALPHA]}})
        copied = store.create_memory({'memory_key': 'review-copy', 'canonical_text': 'Synthetic restricted observation', 'status': 'active', 'domain': 'unknown', 'sensitivity': 'public', 'metadata_json': {'source_id': str(source['id']), 'project_scope': [ALPHA]}})
        summary = store.create_memory({'memory_key': 'review-summary', 'canonical_text': 'Summary of the synthetic restricted observation', 'status': 'active', 'domain': 'unknown', 'sensitivity': 'public', 'metadata_json': {'consolidation': {'cluster_member_ids': [str(copied['id'])]}, 'project_scope': [ALPHA]}})
        assert copied['sensitivity'] == 'confidential'
        assert summary['sensitivity'] == 'confidential', f"two-hop child persisted as {summary['domain']}/{summary['sensitivity']}"


def test_unresolved_insert_still_succeeds_conservatively(tmp_path: Path) -> None:
    with _vault(tmp_path / "missing.sqlite3") as conn:
        store = SQLiteVNextStore(conn, USER)
        row = store.create_memory({"memory_key": "missing", "canonical_text": "summary", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": ["missing"]}, "project_scope": [ALPHA]}})
    assert row["sensitivity"] == "regulated"
    assert row["metadata_json"]["project_floor"] == [ALPHA]


def test_relabel_reads_the_other_branch_ancestry(tmp_path: Path) -> None:
    with _vault(tmp_path / "branches.sqlite3") as conn:
        store = SQLiteVNextStore(conn, USER)
        copies = []
        sources = []
        for suffix in ("a", "b"):
            source = store.create_source({"source_type": "note", "title": suffix, "content_hash": suffix, "domain": "unknown", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}})
            sources.append(source)
            copies.append(store.create_memory({"memory_key": suffix, "canonical_text": suffix, "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"source_id": source["id"]}}))
        summary = store.create_memory({"memory_key": "summary", "canonical_text": "summary", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [row["id"] for row in copies]}}})
        store.update_source(source_id=str(sources[0]["id"]), patch={"domain": "health", "sensitivity": "confidential"}, actor_type="user")
        stored = store.get_memory(str(summary["id"]))
    assert stored["domain"] == "health"
    assert stored["sensitivity"] == "confidential"


def test_sqlite_label_lookup_preserves_uuid_aliases_and_kinds(tmp_path: Path) -> None:
    from uuid import UUID
    with _vault(tmp_path / "aliases.sqlite3") as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({"source_type": "note", "title": "alias", "content_hash": "alias", "domain": "health", "sensitivity": "confidential"})
        raw = "{" + str(source["id"]).upper() + "}"
        conn.execute("UPDATE sources SET id = ? WHERE id = ?", (raw, source["id"]))
        found = store.read_label_rows("source", [str(UUID(raw))])
        assert [row["id"] for row in found] == [raw]
        assert store.read_label_rows("memory", [str(UUID(raw))]) == []
        copy = store.create_memory({"memory_key": "alias-copy", "canonical_text": "copy", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"source_id": str(UUID(raw))}})
    assert copy["domain"] == "health"
    assert copy["sensitivity"] == "confidential"


def test_alias_twins_cannot_choose_a_public_source(tmp_path: Path) -> None:
    with _vault(tmp_path / "twins.sqlite3") as conn:
        store = SQLiteVNextStore(conn, USER)
        restricted = store.create_source({"source_type": "note", "title": "restricted", "content_hash": "restricted", "domain": "health", "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}})
        public = store.create_source({"source_type": "note", "title": "public", "content_hash": "public", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": ["prj_" + "b" * 16]}})
        conn.execute("UPDATE sources SET id = ? WHERE id = ?", (str(restricted["id"]).replace("-", "").upper(), public["id"]))
        copy = store.create_memory({"memory_key": "twins-copy", "canonical_text": "copy", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"source_id": str(restricted["id"])}})
    assert copy["sensitivity"] == "regulated"
    assert copy["domain"] == "health"
    assert set(copy["metadata_json"]["project_floor"]) == {ALPHA, "prj_" + "b" * 16}


def test_relabel_regulates_legacy_summary_with_missing_ancestry(tmp_path: Path) -> None:
    with _vault(tmp_path / "missing-branch.sqlite3") as conn:
        store = SQLiteVNextStore(conn, USER)
        source = store.create_source({"source_type": "note", "title": "input", "content_hash": "missing-branch", "domain": "unknown", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}})
        copy = store.create_memory({"memory_key": "copy", "canonical_text": "copy", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"source_id": source["id"]}})
        with without_insert_floor():
            summary = store.create_memory({"memory_key": "summary", "canonical_text": "summary", "status": "active", "domain": "unknown", "sensitivity": "public", "metadata_json": {"consolidation": {"cluster_member_ids": [copy["id"], "missing"]}}})
        store.update_source(source_id=str(source["id"]), patch={"domain": "health", "sensitivity": "confidential"}, actor_type="user")
        row = store.get_memory(str(summary["id"]))
    assert row["domain"] == "health"
    assert row["sensitivity"] == "regulated"
    assert ALPHA in row["metadata_json"]["project_floor"]
