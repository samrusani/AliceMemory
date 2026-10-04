"""Review reproductions and boundary guards for Markdown source retirement."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.onramp import main as cli
from alicebot_api.source_supersede import SupersedePolicy, UNSUPPORTED_SUPERSEDE
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService, VNextCaptureValidationError
from alicebot_api.vnext_stores.sqlite.source_retirement import close_mention_edges
from alicebot_api import vault_sleep as sleep
from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault, _folder
from tests.unit.test_source_supersede import run_import


def _read(db, sql, parameters=()):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, parameters).fetchall()


def linked_memory(store, sid, *, status='candidate', chunk_only=False, event_only=False):
    memory = store.create_memory({'memory_key':'review.'+str(uuid4()), 'status':status,
        'memory_type':'semantic', 'canonical_text':'The older amber statement.',
        'value':{'text':'The older amber statement.'}, 'source_event_ids':[sid] if event_only else []})
    mid = str(memory['id'])
    if status == 'archived':
        store.conn.execute("UPDATE memories SET deleted_at='2020-01-01T00:00:00Z' WHERE id=?", (mid,))
    if not event_only:
        link = {'target_type':'memory', 'target_id':mid, 'quote':'The older amber statement.'}
        link['source_chunk_id' if chunk_only else 'source_id'] = store.list_source_chunks(sid)[0]['id'] if chunk_only else sid
        store.create_provenance_link(link)
    return mid


def test_replace_rejects_candidate_with_only_a_chunk_link(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, chunk_only=True)
    (folder/'note.md').write_text('The current copper statement.')
    assert run_import(db, folder, supersede=True).superseded
    assert _read(db, 'SELECT status FROM memories WHERE id=?', (mid,)) == [('rejected',)]


def inject_sleep_pause(monkeypatch, action):
    """Pause after the read connection closes, before the prepared rows publish."""
    original = sleep.sqlite_user_connection
    paused = []
    @contextmanager
    def connection(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield conn
        if not paused:
            paused.append(True)
            action()
    monkeypatch.setattr(sleep, 'sqlite_user_connection', connection)
    return paused


def test_sleep_cannot_publish_a_source_replaced_after_its_read(tmp_path, monkeypatch):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    old = run_import(db, folder).source_ids[0]
    def replace():
        (folder/'note.md').write_text('The current copper statement.')
        assert run_import(db, folder, supersede=True).superseded
    paused = inject_sleep_pause(monkeypatch, replace)
    receipt = sleep.run_local_vault_sleep(db, user_id=USER_ID)
    assert paused
    rows = sleep.load_sleep_proposals(sleep.sleep_proposals_path(db))
    assert not any(row['source_id'] == old for row in rows)
    assert 'proposals written: 0' in receipt


def test_batch_dedupe_respects_inherited_labels_and_changed_warning(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, a='The shared amber statement.', z='The old private statement.')
    run_import(db, folder/'a.md', sensitivity='public')
    run_import(db, folder/'z.md', sensitivity='private')
    (folder/'z.md').write_text('The shared amber statement.')
    result = run_import(db, folder)
    assert (result.imported_count, result.duplicate_count, result.changed_files_count) == (1, 1, 1)
    assert '--supersede' in result.to_record()['replacement_hint']
    assert _read(db, "SELECT sensitivity FROM sources WHERE id=?", (result.source_ids[0],)) == [('private',)]


def test_user_entity_survives_retirement_and_closed_edges_do_not_decrement_twice(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='Alice Marlow holds the amber lantern.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("UPDATE vnext_entities SET metadata_json=json_set(metadata_json,'$.created_by','user')")
        before = conn.execute('SELECT id,mention_count FROM vnext_entities').fetchall()
        assert before and all(row['mention_count'] > 0 for row in before)
    (folder/'note.md').write_text('The copper lantern is upstairs.')
    run_import(db, folder, supersede=True)
    with sqlite_user_connection(db, USER_ID) as conn:
        after = conn.execute('SELECT id,name,mention_count,deleted_at FROM vnext_entities').fetchall()
        assert len(after) == len(before)
        assert all(row['name'] == 'Alice Marlow' and not row['deleted_at'] and row['mention_count'] == 0 for row in after)
        # A later unrelated observation must survive cleanup of a closed edge.
        conn.execute('UPDATE vnext_entities SET mention_count=7')
        close_mention_edges(SQLiteVNextStore(conn, USER_ID), 'source', sid, '2030-01-01T00:00:00Z')
        assert all(row['mention_count'] == 7 for row in conn.execute('SELECT mention_count FROM vnext_entities'))


def test_sleep_prune_preserves_other_sources_and_other_users(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, a='The amber statement.', b='The bronze statement.')
    old, other = [run_import(db, folder/f'{name}.md').source_ids[0] for name in ('a','b')]
    rows = [{'user_id':USER_ID,'source_id':old,'excerpt':'old'},
            {'user_id':USER_ID,'source_id':other,'excerpt':'keep'},
            {'user_id':str(uuid4()),'source_id':old,'excerpt':'other user'}]
    sidecar = sleep.sleep_proposals_path(db)
    sidecar.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    (folder/'a.md').write_text('The copper replacement.')
    run_import(db, folder/'a.md', supersede=True)
    assert sleep.load_sleep_proposals(sidecar) == rows[1:]


def test_bare_replace_inherits_newest_label_and_still_checks_older_stricter_copy(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The first amber statement.')
    first = run_import(db, folder, domain='personal', sensitivity='private').source_ids[0]
    (folder/'note.md').write_text('The next amber statement.')
    second = run_import(db, folder, domain='personal', sensitivity='internal').source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        conn.execute("UPDATE sources SET captured_at='2020-01-01T00:00:00Z' WHERE id=?", (first,))
        conn.execute("UPDATE sources SET captured_at='2021-01-01T00:00:00Z' WHERE id=?", (second,))
    (folder/'note.md').write_text('The latest copper statement.')
    refused = run_import(db, folder, supersede=True)
    assert refused.status == 'refused' and refused.refused[0]['reason'] == 'looser_classification'
    allowed = run_import(db, folder, supersede=True, allow_looser_classification=True)
    assert _read(db, 'SELECT domain,sensitivity FROM sources WHERE id=?', (allowed.source_ids[0],)) == [('personal','internal')]


def test_path_lookup_is_bound_to_the_store_user(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The amber statement.')
    run_import(db, folder)
    other = str(uuid4())
    with sqlite_user_connection(db, USER_ID) as conn:
        ensure_sqlite_user(conn, other, 'other@example.test')
        assert SQLiteVNextStore(conn, other).markdown_sources_by_path() == {}


@pytest.mark.parametrize('door', ['capture', 'import'])
def test_each_unsupported_backend_refusal_is_independent(tmp_path, monkeypatch, door):
    from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore
    service = VNextCaptureService(InMemoryVNextCaptureStore())
    calls = []
    # Removing either guard must reach its own sentinel, not the other guard.
    if door == 'capture':
        monkeypatch.setattr(service, '_capture_source', lambda *a, **k: calls.append(True))
        payload = SourceCaptureInput(source_type='markdown', connector_name='markdown_folder',
            raw_path=str(tmp_path/'note.md'), raw_text='Amber.', supersede=SupersedePolicy.by_path())
        action = lambda: service.capture_source(payload)
    else:
        monkeypatch.setattr(service, '_import_markdown_folder', lambda *a, **k: calls.append(True))
        action = lambda: service.import_markdown_folder(tmp_path, supersede=True)
    with pytest.raises(VNextCaptureValidationError, match='^'+UNSUPPORTED_SUPERSEDE+'$'):
        action()
    assert not calls


@pytest.mark.parametrize('status', ['candidate','needs_review','rejected','active','accepted','private_only','stale','superseded','archived'])
def test_replace_lists_every_memory_that_keeps_its_text(tmp_path, status):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, status=status)
    (folder/'note.md').write_text('The current copper statement.')
    result = run_import(db, folder, supersede=True)
    assert mid in result.to_record()['memories_citing_replaced']
    assert _read(db, 'SELECT canonical_text FROM memories WHERE id=?', (mid,)) == [('The older amber statement.',)]


@pytest.mark.parametrize('partial', [False, True])
def test_refused_and_partial_import_receipts_exit_one(tmp_path, capsys, partial):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The old private statement.')
    run_import(db, folder, sensitivity='private')
    (folder/'note.md').write_text('The new private statement.')
    if partial:
        (folder/'other.md').write_text('The other statement.')
    assert cli(['import-markdown','--from',str(folder),'--db',str(db),
                '--supersede','--sensitivity','public']) == 1
    record = json.loads(capsys.readouterr().out)
    assert record['status'] == ('partial' if partial else 'refused')
    assert record['refused_count'] == 1
    assert record['imported_count'] == int(partial)


def test_refused_and_kept_file_labels_escape_controls(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The first statement.')
    strange = folder/'note\n\x1b[31m.md'; (folder/'note.md').rename(strange)
    run_import(db, strange, sensitivity='private')
    strange.write_text('The changed statement.')
    refused = run_import(db, strange, supersede=True, sensitivity='public')
    assert refused.refused[0]['file'] == r'note\u000a\u001b[31m.md'
    other = folder/'other.md'; other.write_text('The identical statement.')
    run_import(db, other, sensitivity='private')
    strange.write_text('The identical statement.')
    kept = run_import(db, strange, supersede=True)
    assert kept.kept[0]['file'] == r'note\u000a\u001b[31m.md'


def test_dry_run_keeps_database_bytes_but_can_create_empty_sqlite_sidecars(tmp_path, capsys):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The amber statement.')
    run_import(db, folder)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = db.read_bytes()
    assert cli(['import-markdown','--from',str(folder),'--db',str(db),'--dry-run','--supersede']) == 0
    assert json.loads(capsys.readouterr().out)['dry_run']
    assert db.read_bytes() == before
    wal = Path(str(db)+'-wal')
    assert not wal.exists() or wal.stat().st_size == 0


def test_all_sidecar_writers_take_the_shared_lock(tmp_path, monkeypatch):
    import alicebot_api.vnext_stores.sqlite.source_retirement as retirement
    from alicebot_api.vault_file_lock import vault_file_lock
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The amber statement.')
    run_import(db, folder)
    entered = []
    @contextmanager
    def tracked(path):
        entered.append(path)
        with vault_file_lock(path):
            yield
    monkeypatch.setattr(sleep, 'vault_file_lock', tracked)
    monkeypatch.setattr(retirement, 'vault_file_lock', tracked)
    sleep.run_local_vault_sleep(db, user_id=USER_ID)
    assert len(entered) == 2  # Publication and the reentrant file writer.
    entered.clear()
    (folder/'note.md').write_text('The copper replacement.')
    run_import(db, folder, supersede=True)
    assert len(entered) == 2  # Retirement and the same file writer.


def test_sidecar_lock_serializes_processes_and_is_reentrant(tmp_path):
    import os
    import select
    import subprocess
    import sys
    from alicebot_api.vault_file_lock import vault_file_lock
    path = tmp_path/'sidecar.jsonl'
    code = '''
from pathlib import Path
import sys
from alicebot_api.vault_file_lock import vault_file_lock
print('ready', flush=True)
with vault_file_lock(Path(sys.argv[1])):
    print('acquired', flush=True)
'''
    env = {**os.environ, 'PYTHONPATH':str(Path(__file__).parents[2]/'apps/api/src')}
    with vault_file_lock(path), vault_file_lock(path):
        process = subprocess.Popen([sys.executable,'-c',code,str(path)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline().strip() == 'ready'
            assert not select.select([process.stdout], [], [], 0.15)[0]
        except BaseException:
            process.kill(); process.wait()
            raise
    output, errors = process.communicate(timeout=15)
    assert process.returncode == 0 and output.strip() == 'acquired', errors
    assert path.with_name(path.name+'.lock').stat().st_mode & 0o777 == 0o600


def test_replacement_receipt_lists_more_than_twenty_retained_memories(tmp_path):
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        ids = {linked_memory(store, sid, status='accepted') for _ in range(21)}
        hidden = linked_memory(store, sid)
        store.redact_memory_bundle(memory_id=hidden, project_update_artifacts=[], actor_type='user')
    (folder/'note.md').write_text('The current copper statement.')
    result = run_import(db, folder, supersede=True)
    assert set(result.to_record()['memories_citing_replaced']) == ids


@pytest.mark.parametrize('status', ['active','candidate'])
def test_owner_saved_quote_readers_keep_the_replaced_source_audit(tmp_path, monkeypatch, status):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    monkeypatch.delenv('ALICE_AGENT_API_KEY', raising=False)
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS', '1')
    db = _vault(tmp_path); folder = _folder(tmp_path, note='The older amber statement.')
    sid = run_import(db, folder).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        mid = linked_memory(SQLiteVNextStore(conn, USER_ID), sid, status=status)
    (folder/'note.md').write_text('The current copper statement.')
    run_import(db, folder, supersede=True)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=USER_ID)
    doors = [('alice_memory_review', {'review_item_id':mid}), ('alice_explain', {'memory_id':mid})]
    if status == 'active':
        doors.append(('alice_context_pack', {'query':'older amber statement', 'max_tokens':4000}))
    def has_link(node):
        if isinstance(node, dict):
            return (node.get('source_id') == sid and 'The older amber statement.' in str(node.get('quote'))) or any(has_link(v) for v in node.values())
        return isinstance(node, list) and any(has_link(v) for v in node)
    for name, arguments in doors:
        assert has_link(call_mcp_tool(context, name=name, arguments=arguments)), name
