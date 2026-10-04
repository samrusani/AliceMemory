"""Deletion review reproductions, including live FTS pages and retained text."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import uuid4

import pytest

from alicebot_api import vault_sleep as sleep
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_doctor import compile_local_vault_doctor
from alicebot_api.vnext_stores.sqlite.source_retirement import REMOVAL_MARKER
from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault, _folder
from tests.unit.test_source_commands import command
from tests.unit.test_source_review_fixes import linked_memory, inject_sleep_pause, _read
from tests.unit.test_source_supersede import run_import


@pytest.mark.parametrize('status', ['candidate', 'active'])
def test_chunk_only_links_are_counted_and_scrubbed(tmp_path, capsys, status):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, status=status, chunk_only=True)
    assert command(db, 'delete', sid) == 2
    preview = json.loads(capsys.readouterr().out)['would_delete'][0]
    assert command(db, 'delete', sid, '--yes') == 0
    receipt = json.loads(capsys.readouterr().out)['deleted'][0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        actual = (preview['candidate_memories'], preview['provenance_quotes'],
                  receipt['candidate_memories'], receipt['provenance_quotes'],
                  store.memory_redaction_bundle_is_exact(mid, []))
        count = int(status == 'candidate')
        assert actual == (count, 1, count, 1, status == 'candidate')
        quote = conn.execute('SELECT quote FROM provenance_links WHERE target_id=?', (mid,)).fetchone()['quote']
        assert 'older amber' not in quote
        if status == 'active':
            assert quote == REMOVAL_MARKER
            assert preview['memories_citing_replaced'] == receipt['memories_citing_replaced'] == [mid]


@pytest.mark.parametrize('operation', ['delete','prune'])
def test_sleep_cannot_republish_after_source_scrub(tmp_path, monkeypatch, capsys, operation):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    def retire():
        if operation == 'prune':
            (folder/'note.md').write_text('The current copper statement.')
            run_import(db, folder, supersede=True)
            assert command(db, 'prune', '--superseded', '--yes') == 0
        else:
            assert command(db, 'delete', sid, '--yes') == 0
        capsys.readouterr()
    inject_sleep_pause(monkeypatch, retire)
    sleep.run_local_vault_sleep(db, user_id=USER_ID)
    assert not any(row['source_id'] == sid for row in sleep.load_sleep_proposals(sleep.sleep_proposals_path(db)))


def fts_fixture(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='Quenvor Plintax keeps the zorplint record.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        memory = store.create_memory({'memory_key':'review.fts', 'status':'candidate', 'memory_type':'semantic',
            'canonical_text':'Orveth Zaltrix keeps the vespnork record.',
            'value':{'text':'Orveth Zaltrix keeps the vespnork record.'}})
        store.create_provenance_link({'target_type':'memory', 'target_id':memory['id'],
                                     'source_id':sid, 'quote':'Quenvor Plintax keeps the zorplint record.'})
    return db, folder, sid


FTS_WORDS = (b'quenvor', b'plintax', b'zorplint', b'orveth', b'zaltrix', b'vespnork')


def assert_no_removed_bytes(path):
    data = path.read_bytes().lower() if path.exists() else b''
    assert not [word.decode() for word in FTS_WORDS if word in data], path.name


@pytest.mark.parametrize('operation', ['delete','prune'])
def test_vacuumed_copy_has_no_removed_words_in_live_fts_pages(tmp_path, capsys, operation):
    db, folder, sid = fts_fixture(tmp_path)
    if operation == 'prune':
        (folder/'note.md').write_text('The current copper statement.')
        run_import(db, folder, supersede=True)
        assert command(db, 'prune', '--superseded', '--yes') == 0
    else:
        assert command(db, 'delete', sid, '--yes') == 0
    capsys.readouterr()
    copy = tmp_path/'vacuumed.db'
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(copy)) as target:
        source.backup(target)
        target.execute('VACUUM')
    assert_no_removed_bytes(copy)


def test_documented_vacuum_and_checkpoint_command_removes_old_bytes(tmp_path, capsys):
    db, _folder_path, sid = fts_fixture(tmp_path)
    assert command(db, 'delete', sid, '--yes') == 0
    receipt = json.loads(capsys.readouterr().out)
    binary = shutil.which('sqlite3')
    assert binary, 'The documented sqlite3 command must be available for this check'
    result = subprocess.run([binary, str(db), 'VACUUM; PRAGMA wal_checkpoint(TRUNCATE);'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert_no_removed_bytes(db)
    assert_no_removed_bytes(Path(str(db)+'-wal'))
    assert 'VACUUM' in receipt['retained_data'] and 'stop' in receipt['retained_data'].lower()
    root = Path(__file__).parents[2]
    for file in ('docs/integrations/importers.md', 'docs/integrations/cli.md'):
        text = (root/file).read_text()
        assert 'sqlite3 <data-dir>/memory.db "VACUUM; PRAGMA wal_checkpoint(TRUNCATE);"' in text
        assert 'stop every program' in text.lower()


def test_scrub_explicitly_enables_secure_delete(tmp_path):
    db, _folder_path, sid = fts_fixture(tmp_path)
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute('PRAGMA secure_delete=OFF')
        assert conn.execute('PRAGMA secure_delete').fetchone()['secure_delete'] == 0
        SQLiteVNextStore(conn, USER_ID).scrub_source(sid)
        assert conn.execute('PRAGMA secure_delete').fetchone()['secure_delete'] == 1


@pytest.mark.parametrize('status', ['active','accepted','private_only','superseded','stale','archived'])
def test_preview_and_receipt_list_every_retained_status(tmp_path, capsys, status):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, status=status)
    assert command(db, 'delete', sid) == 2
    preview = json.loads(capsys.readouterr().out)['would_delete'][0]
    assert command(db, 'delete', sid, '--yes') == 0
    receipt = json.loads(capsys.readouterr().out)['deleted'][0]
    assert preview['memories_citing_replaced'] == [mid]
    assert receipt['memories_citing_replaced'] == [mid]
    assert _read(db, 'SELECT canonical_text FROM memories WHERE id=?', (mid,)) == [('The older amber statement.',)]


@pytest.mark.parametrize('status', ['candidate','needs_review','rejected'])
def test_every_uncommitted_status_is_redacted(tmp_path, capsys, status):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, status=status)
    assert command(db, 'delete', sid) == 2
    preview = json.loads(capsys.readouterr().out)['would_delete'][0]
    assert command(db, 'delete', sid, '--yes') == 0
    receipt = json.loads(capsys.readouterr().out)['deleted'][0]
    assert preview['candidate_memories'] == receipt['candidate_memories'] == 1
    assert preview['memories_citing_replaced'] == receipt['memories_citing_replaced'] == []
    with sqlite_user_connection(db, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).memory_redaction_bundle_is_exact(mid, [])


def test_preview_and_receipt_do_not_silently_cap_retained_ids(tmp_path, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        ids = {linked_memory(store, sid, status='active') for _ in range(21)}
        hidden = linked_memory(store, sid)
        store.redact_memory_bundle(memory_id=hidden, project_update_artifacts=[], actor_type='user')
    assert command(db, 'delete', sid) == 2
    preview = json.loads(capsys.readouterr().out)['would_delete'][0]
    assert command(db, 'delete', sid, '--yes') == 0
    receipt = json.loads(capsys.readouterr().out)['deleted'][0]
    assert set(preview['memories_citing_replaced']) == set(receipt['memories_citing_replaced']) == ids
    assert preview['candidate_memories'] == receipt['candidate_memories'] == 0


def test_event_id_only_candidate_is_redacted(tmp_path, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, event_only=True)
    assert command(db, 'delete', sid, '--yes') == 0
    assert json.loads(capsys.readouterr().out)['deleted'][0]['candidate_memories'] == 1
    with sqlite_user_connection(db, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).memory_redaction_bundle_is_exact(mid, [])


def test_prune_never_selects_live_source_with_stray_marker(tmp_path, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The live amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("UPDATE sources SET metadata_json=json_set(metadata_json,'$.superseded_by',?,'$.superseded_at','2000-01-01T00:00:00Z')", (str(uuid4()),))
    assert command(db, 'prune', '--superseded', '--yes') == 0
    assert json.loads(capsys.readouterr().out)['deleted_count'] == 0
    assert _read(db, 'SELECT deleted_at FROM sources WHERE id=?', (sid,)) == [(None,)]


def test_doctor_prints_the_sqlite_flagged_source_remedy(tmp_path):
    from tests.unit.test_sqlite_source_import import _token
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The amber statement.')
    run_import(db, folder)
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute('UPDATE sources SET title=?', (_token(),))
    report = compile_local_vault_doctor(db, user_id=USER_ID)
    assert 'flagged sources: 1' in report
    assert 'Remove flagged sources with alice-memory sources delete <id>.' in report


def test_doctor_counts_replaced_sources_without_loading_rows(tmp_path, monkeypatch):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    run_import(db, folder)
    (folder/'note.md').write_text('The current copper statement.')
    run_import(db, folder, supersede=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Doctor must use a count query, not load the replaced rows')
    monkeypatch.setattr(SQLiteVNextStore, 'prunable_sources', forbidden)
    assert 'superseded sources: 1' in compile_local_vault_doctor(db, user_id=USER_ID)


def test_failed_candidate_scrub_reports_sidecar_loss_truthfully(tmp_path, monkeypatch, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        linked_memory(SQLiteVNextStore(conn, USER_ID), sid)
    sleep.run_local_vault_sleep(db, user_id=USER_ID)
    assert sleep.load_sleep_proposals(sleep.sleep_proposals_path(db))
    def fail(*args, **kwargs):
        raise ValueError('injected failure')
    monkeypatch.setattr(SQLiteVNextStore, 'redact_memory_bundle', fail)
    assert command(db, 'delete', sid, '--yes') == 1
    message = json.loads(capsys.readouterr().out)['error']['message']
    assert 'rolled back' in message and 'Sleep proposals may have been removed' in message
    assert 'alice-memory sleep' in message
    assert _read(db, 'SELECT deleted_at FROM sources WHERE id=?', (sid,)) == [(None,)]
    assert sleep.load_sleep_proposals(sleep.sleep_proposals_path(db)) == []


def test_corrupt_sidecar_reports_recovery_steps(tmp_path, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    sleep.sleep_proposals_path(db).write_text('{invalid\n')
    assert command(db, 'delete', sid, '--yes') == 1
    message = json.loads(capsys.readouterr().out)['error']['message'].lower()
    assert all(word in message for word in ('stop','sleep_proposals.jsonl','move','alice-memory sleep'))
    assert _read(db, 'SELECT deleted_at FROM sources WHERE id=?', (sid,)) == [(None,)]


def test_out_of_range_age_is_clearly_refused(tmp_path, capsys):
    db = _vault(tmp_path)
    assert command(db, 'prune', '--superseded', '--older-than', str(2**63), '--yes') == 1
    error = json.loads(capsys.readouterr().out)['error']
    assert error['code'] == 'source_request_refused' and 'between 0 and' in error['message']


def test_non_uuid_source_id_is_a_request_error_not_confirmation(tmp_path, capsys):
    db = _vault(tmp_path)
    try:
        code = command(db, 'delete', 'not-a-uuid')
    except SystemExit as exc:
        code = exc.code
    output = capsys.readouterr()
    assert code == 1
    assert 'valid UUID' in json.loads(output.out)['error']['message']


def test_direct_scrub_also_removes_obsolete_fts_postings(tmp_path):
    db, _folder_path, sid = fts_fixture(tmp_path)
    with sqlite_user_connection(db, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).scrub_source(sid)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('VACUUM')
    assert_no_removed_bytes(db)


def test_index_merge_failure_rolls_back_database_scrub(tmp_path, monkeypatch, capsys):
    from alicebot_api import source_commands
    db, _folder_path, sid = fts_fixture(tmp_path)
    with closing(sqlite3.connect(db)) as conn:
        before = list(conn.iterdump())
    def fail(store):
        raise ValueError('injected index failure')
    monkeypatch.setattr(source_commands, 'optimize_scrub_indexes', fail)
    assert command(db, 'delete', sid, '--yes') == 1
    capsys.readouterr()
    with closing(sqlite3.connect(db)) as conn:
        assert list(conn.iterdump()) == before
