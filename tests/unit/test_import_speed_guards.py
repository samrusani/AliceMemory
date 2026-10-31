"""Guards of the import speed-up whose removal must fail a named test.

Each test here pins one guard of the speed-up (the import-scoped door memo,
the per-call prep cache, the row-lookup cache and its supersede lockout, the
content-hash lookup's live-row filter, the whole-source credential check,
and the lookup's identity revalidation). The PR body lists each guard, the
edit that removes it, and the observed failure; the mutation manifest
replays each removal as a killed case.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import alicebot_api.vnext_capture as vc
from alicebot_api.onramp import bootstrap_database, resolve_db_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_capture import (
    SourceCaptureInput,
    VNextCaptureService,
    _SourceLookupCache,
)

USER_ID = "00000000-0000-0000-0000-000000000001"


def _vault(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path / "data"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _import(database: Path, folder: Path, **kwargs: object) -> object:
    with sqlite_user_connection(database, USER_ID) as conn:
        return VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, **kwargs)


def _live_titles(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as conn:
        return sorted(
            title for (title,) in conn.execute("SELECT title FROM sources WHERE deleted_at IS NULL")
        )


def test_the_row_cache_stays_off_under_supersede(tmp_path: Path) -> None:
    """Guard: the lookup cache is disabled for a --supersede import.

    Inside a --supersede run rows are retired while the run is going; the
    cache would answer a later file with the row as it stood before the
    retirement. Run 1 stores b_p (text X); run 2 rewrites b_p to Y and adds
    two copies of X. Main: a_q is a duplicate of the run-1 row, b_p's new
    text retires it, c_r imports, and the live set is b_p and c_r. A cache
    kept on answers both copies of X with the run-1 row, c_r never imports,
    and the live set is b_p alone.
    """

    shared = "# Shared\n\nthe shared body token sharedX\n"
    changed = "# B new\n\nthe b body token newY\n"
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "b_p.md").write_text(shared, encoding="utf-8")
    database = _vault(tmp_path / "v")
    first = _import(database, folder)
    assert first.imported_count == 1
    (folder / "b_p.md").write_text(changed, encoding="utf-8")
    (folder / "a_q.md").write_text(shared, encoding="utf-8")
    (folder / "c_r.md").write_text(shared, encoding="utf-8")
    second = _import(database, folder, supersede=True)
    assert second.status == "ok", second.status
    assert second.imported_count == 2, (second.imported_count, second.duplicate_count)
    assert second.duplicate_count == 1, second.duplicate_count
    assert _live_titles(database) == ["b_p", "c_r"], _live_titles(database)


def test_a_soft_deleted_row_is_not_a_content_hash_duplicate(tmp_path: Path) -> None:
    """Guard: ``AND deleted_at IS NULL`` in get_sources_by_content_hash.

    A soft-deleted row keeps its content hash; a fresh capture of the same
    text must store a new source, not answer with the deleted row.
    """

    text = "# Deleted\n\nthe deleted body token dd\n"
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "n.md").write_text(text, encoding="utf-8")
    database = _vault(tmp_path / "v")
    first = _import(database, folder)
    assert first.imported_count == 1
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z'")
        conn.commit()
    second = _import(database, folder)
    assert second.status == "ok", second.status
    assert second.imported_count == 1, (second.imported_count, second.duplicate_count)
    assert _live_titles(database) == ["n"], _live_titles(database)


def test_the_whole_source_check_runs_for_a_large_file(tmp_path: Path) -> None:
    """Guard: the whole-source door call runs whatever the file's size is.

    A key split across two lines is invisible to the per-line unit filter
    and only the whole-source check refuses it; a file well past any memo
    key bound must still be refused, not skipped as too large to check.
    """

    folder = tmp_path / "folder"
    folder.mkdir()
    filler = "# Big\n\n" + ("filler paragraph with ordinary words about the archive\n" * 100)
    assert len(filler) > 5_000
    (folder / "big.md").write_text(
        filler + "api key\n = hunter2value and the note continues.\n", encoding="utf-8"
    )
    database = _vault(tmp_path / "v")
    result = _import(database, folder)
    assert result.skipped_credentials == 1, result.skipped_credentials
    assert result.imported_count == 0, result.imported_count
    assert _live_titles(database) == [], _live_titles(database)


def test_the_floor_variant_scan_refuses_on_its_own() -> None:
    """Guard: the floor's decoded variants are computed in the prep cache.

    The two halves of a base64-wrapped database URL are clean alone: the
    legacy half runs per string and sees nothing, while the floor decodes
    the joined surface and refuses. Emptying the cached variants (so the
    floor's decoded scan never sees the URL) answers None and fails here.
    """

    from alicebot_api.legacy_credential_check import commit_door_fields_verdict, commit_gate_refuses

    secret = base64.b64encode(b"DATABASE_URL=postgresql://alice:hunter2pass@db.internal/alice").decode()
    # The legacy half does not refuse it: the floor's decoded scan is the
    # only place this fixture is caught, so emptying the cached variants
    # answers None and fails here.
    assert commit_gate_refuses("", secret) is False
    assert commit_door_fields_verdict("note", secret) == "credential"


def test_no_memo_survives_the_import_across_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the memo is per import, wherever it is stored.

    A memo kept in a module-level dict that only the instance attribute's
    reset clears would answer the second import's identical unit lines from
    the first import's entries, and the door would not run at all on them.
    The second import here runs with the door counted: with the memo truly
    import-scoped every unit line reaches the door again.
    """

    text = "# Same\n\nFact: the access id is AKIAAAAAAAAAAAAA inline in the sentence.\n"
    first_folder = tmp_path / "one"
    first_folder.mkdir()
    (first_folder / "n.md").write_text(text, encoding="utf-8")
    second_folder = tmp_path / "two"
    second_folder.mkdir()
    # The flagged line repeats exactly; the extra paragraph keeps the text
    # different, so the second import is not a content-hash duplicate and
    # its unit lines reach the filter again.
    (second_folder / "n.md").write_text(
        text + "\nExtra paragraph unique to the second folder with token zz.\n", encoding="utf-8"
    )
    database = _vault(tmp_path / "v")
    assert _import(database, first_folder).imported_count == 1

    calls: list[tuple[object, ...]] = []
    real = vc.commit_door_fields_verdict

    def counting(*fields: object) -> str | None:
        calls.append(fields)
        return real(*fields)

    monkeypatch.setattr(vc, "commit_door_fields_verdict", counting)
    assert _import(database, second_folder).imported_count == 1
    assert calls, "the door never ran on the second import"
    assert any(
        isinstance(field, str) and "AKIA" in field for fields in calls for field in fields
    ), "the repeated flagged line was answered from a surviving memo"


def test_the_whole_source_site_never_reaches_the_memo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the whole-source check runs the door directly, not through the memo.

    The whole-source call's key would carry the whole filtered file text,
    which the memo must never hold, and each file's text is seen here
    exactly once per import, so memoizing it could never hit. A memoized
    whole-source site shows up as a second call-site name on the memo.
    """

    folder = tmp_path / "folder"
    folder.mkdir()
    for name in ("a", "b"):
        (folder / f"{name}.md").write_text(f"# {name}\n\nthe {name} body token {name}{name}\n", encoding="utf-8")
    database = _vault(tmp_path / "v")
    sites: list[str] = []
    real = vc._door_fields_verdict_memoized

    def spy(memo, site, *fields):
        sites.append(site)
        return real(memo, site, *fields)

    monkeypatch.setattr(vc, "_door_fields_verdict_memoized", spy)
    assert _import(database, folder).imported_count == 2
    assert sites and set(sites) == {"unit_verdict"}, sites


def test_two_memo_sites_never_share_an_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the memo key carries the call site (the exact-input memo).

    Two sites asked for the same field value must each run the door: a key
    that drops the site answers the second site from the first site's
    entry, which the counted door here exposes as one call instead of two.
    """

    real = vc.commit_door_fields_verdict
    calls: list[object] = []

    def fake(*fields: object) -> str | None:
        calls.append(fields[0] if fields else None)
        return "credential" if len(calls) == 1 else None

    monkeypatch.setattr(vc, "commit_door_fields_verdict", fake)
    memo = {"entries": {}, "chars": 0}
    first = vc._door_fields_verdict_memoized(memo, "unit_verdict", "one line")
    second = vc._door_fields_verdict_memoized(memo, "other_site", "one line")
    assert first == "credential" and second is None
    assert calls == ["one line", "one line"], calls


def test_the_dedupe_hit_still_validates_the_row_envelope(tmp_path: Path) -> None:
    """Guard: the cache's dedupe path revalidates with ``source_capture_identity_matches``.

    A row whose stored dedupe key still matches the capture but whose
    envelope was reclassified must not be served as a duplicate: the
    identity check refuses it, and the capture then fails in the store's
    atomic dedupe rather than answering with the stale row.
    """

    from alicebot_api.store import ContinuityStoreInvariantError

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "n.md").write_text("# N\n\nthe n body token nn\n", encoding="utf-8")
    database = _vault(tmp_path / "v")
    first = _import(database, folder, domain="personal")
    assert first.imported_count == 1
    with closing(sqlite3.connect(database)) as conn:
        # The envelope moves to another domain and the preserved raw text is
        # gone (an upgraded row's shape), while the stored dedupe key keeps
        # the old one: the recompute guard cannot reject this row (there is
        # nothing to recompute from), so the identity check is what refuses it.
        row = conn.execute("SELECT metadata_json FROM sources WHERE title = 'n'").fetchone()
        metadata = json.loads(row[0])
        metadata.pop("raw_text", None)
        conn.execute(
            "UPDATE sources SET domain = 'professional', metadata_json = ? WHERE title = 'n'",
            (json.dumps(metadata),),
        )
        conn.commit()
    with sqlite_user_connection(database, USER_ID) as conn:
        service = VNextCaptureService(SQLiteVNextStore(conn, USER_ID))
        # The cache path is the one whose dedupe hit the identity check
        # revalidates; an import sets it the same way.
        service._source_lookup_cache = _SourceLookupCache()
        with pytest.raises(ContinuityStoreInvariantError):
            service.capture_source(
                SourceCaptureInput(
                    source_type="markdown",
                    title="n",
                    raw_text="# N\n\nthe n body token nn\n",
                    raw_path=str(folder / "n.md"),
                    connector_name="markdown_folder",
                    external_id="n.md",
                    domain="personal",
                    sensitivity="unknown",
                    extract_candidates=False,
                )
            )
    assert _live_titles(database) == ["n"], _live_titles(database)


def test_the_cache_records_own_insertions_by_dedupe(tmp_path: Path) -> None:
    """Guard: the inserted source joins the cache's dedupe index.

    On a store whose dedupe lookup the import cannot call, the run's own
    insertions are the only way a later capture of the same identity
    answers as a duplicate; the index must carry them.
    """

    text = "# Dedupe\n\nthe dedupe body token kk\n"
    database = _vault(tmp_path / "v")
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        service = VNextCaptureService(store)
        service._source_lookup_cache = _SourceLookupCache()
        store.get_source_by_dedupe_key = None  # type: ignore[method-assign]
        first = service.capture_source(
            SourceCaptureInput(
                source_type="markdown", title="n", raw_text=text,
                connector_name="markdown_folder", external_id="n.md",
                domain="personal", extract_candidates=False,
            )
        )
        assert first.source_id is not None
        second = service.capture_source(
            SourceCaptureInput(
                source_type="markdown", title="n2", raw_text=text,
                connector_name="markdown_folder", external_id="n2.md",
                domain="personal", extract_candidates=False,
            )
        )
        assert second.duplicate is True, second
        # The dedupe index carries the row itself (not just the negative
        # answer the lookup wrote): on a store without the dedupe lookup
        # the run's own insertion is the only positive entry there.
        assert any(value is not None for value in service._source_lookup_cache.by_dedupe.values()), service._source_lookup_cache.by_dedupe


def test_a_rolled_back_file_leaves_no_row_in_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: the cache takes a source only after the file's writes commit.

    The per-file savepoint rolls a failed file back; a row the cache took
    before the rollback is a row a fresh query would not answer, so after
    the run the cache must hold exactly the sources the vault holds.
    """

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nthe a body token aa\n", encoding="utf-8")
    (folder / "b.md").write_text("# B\n\nthe b body token bb\n", encoding="utf-8")
    database = _vault(tmp_path / "v")

    real_create_chunk = SQLiteVNextStore.create_source_chunk
    state = {"calls": 0}

    def raise_once(self, chunk, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("injected chunk failure")
        return real_create_chunk(self, chunk, **kwargs)

    monkeypatch.setattr(SQLiteVNextStore, "create_source_chunk", raise_once)
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        service = VNextCaptureService(store)
        snapshots: list[set[str]] = []
        real_capture = service._capture_source

        def recording_capture(source_input):
            result = real_capture(source_input)
            cache = service._source_lookup_cache
            if cache is not None:
                snapshots.append({str(row["id"]) for rows in cache.by_hash.values() for row in rows})
            return result

        service._capture_source = recording_capture  # type: ignore[method-assign]
        result = service.import_markdown_folder(folder)
    monkeypatch.undo()
    assert result.failed_count == 1, result.failed_count
    assert result.imported_count == 1, result.imported_count
    assert snapshots, "the cache was never observed"
    with closing(sqlite3.connect(database)) as raw:
        stored = {str(row) for (row,) in raw.execute("SELECT id FROM sources")}
    assert snapshots[-1] == stored, (snapshots[-1], stored)
