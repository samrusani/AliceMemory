"""TS17-TS23 and TS25: owner source commands on synthetic SQLite vaults."""
from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import UUID,uuid4

import pytest

from alicebot_api.onramp import main as cli, _write_export, _KNOWN_COMMANDS, _quarantine_removal_line
from alicebot_api.sqlite_store import SQLiteVNextStore,sqlite_user_connection
from alicebot_api.vault_sleep import sleep_proposals_path
from alicebot_api.vnext_doctor import _flagged_source_remedy
from alicebot_api.vnext_stores.sqlite.source_retirement import REMOVAL_MARKER
from tests.unit.test_source_supersede import run_import,live
from tests.unit.test_importer_per_file_savepoint import USER_ID,_vault,_folder,_read
from tests.unit.test_per_project_no_schema_change import _vault_state

TEXT='Bob Smith keeps the kestrelplum lantern in the loft.'


def command(db,*arguments):
    return cli(['sources',*arguments,'--db',str(db),'--user-id',USER_ID])


def seed(db,folder):
    sid=run_import(db,folder).source_ids[0]
    sidecar=sleep_proposals_path(db)
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':sid,'excerpt':TEXT})+'\n')
    with sqlite_user_connection(db,USER_ID) as conn:
        store=SQLiteVNextStore(conn,USER_ID)
        conn.execute("UPDATE vnext_entities SET aliases=?, metadata_json=json_set(metadata_json,'$.note',?)", (json.dumps(['Bob Smith']),TEXT))
        store.update_source(source_id=sid,patch={'title':TEXT,'author':TEXT,'uri':'https://example.test/kestrelplum',
            'external_id':TEXT})
        store.create_open_loop({'title':TEXT,'description':TEXT,'resolution_note':None,
                               'source_id':sid,'status':'open','metadata_json':{'source_note':TEXT}})
        memory=store.create_memory({'memory_key':'pending.kestrelplum','value':{'text':TEXT},'canonical_text':TEXT,
            'title':TEXT,'summary':TEXT,'status':'candidate','memory_type':'semantic',
            'metadata_json':{'source_id':sid,'quote':TEXT},'source_event_ids':[sid]})
        mid=str(memory['id'])
        store.create_provenance_link({'target_type':'memory','target_id':mid,'source_id':sid,'quote':TEXT})
        store.append_revision({'memory_id':mid,'memory_key':memory['memory_key'],'action':'created',
            'candidate':{'text':TEXT},'new_value':{'text':TEXT},'text_after':TEXT,'metadata_json':{'quote':TEXT}})
    return sid,mid


def test_delete_scrubs_every_owned_copy_and_reimport_links_fresh_entity(tmp_path,capsys,monkeypatch):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from alicebot_api.session_briefing import compile_local_session_brief
    from alicebot_api.project_view import ProjectView
    db=_vault(tmp_path);folder=_folder(tmp_path,note=TEXT)
    sid,mid=seed(db,folder)
    assert _read(db,'SELECT name FROM vnext_entities')
    assert command(db,'delete',sid,'--yes') == 0
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['deleted_count'] == 1 and receipt['deleted'][0]['candidate_memories'] == 1
    assert all(word in receipt['retained_data'] for word in ('events','hash','backups','write-ahead','Committed'))
    assert not sleep_proposals_path(db).read_text()
    assert sleep_proposals_path(db).stat().st_mode & 0o777 == 0o600
    with sqlite_user_connection(db,USER_ID) as conn:
        store=SQLiteVNextStore(conn,USER_ID)
        removed=store.get_sources_by_ids([sid],include_deleted=True)[0]
        assert set(removed['metadata_json']) == {'scrubbed','scrubbed_at'}
        assert all(removed[key] is None for key in ('title','author','uri','raw_path','external_id'))
        assert removed['deleted_at'] and removed['content_hash'] and removed['dedupe_key']
        assert store.memory_redaction_bundle_is_exact(mid,[])
        assert not store.get_source(sid)
        chunks=store._fetch_all('SELECT text FROM source_chunks WHERE source_id=?',(sid,))
        assert chunks and all(row['text'] == REMOVAL_MARKER for row in chunks)
        for table in ('sources','source_chunks','provenance_links','memories','memory_revisions',
                      'graph_edges','vnext_entities','open_loops'):
            blob=json.dumps(store._fetch_all('SELECT * FROM '+table),default=str).casefold()
            assert 'kestrelplum' not in blob and 'bob smith' not in blob,table
        assert not store._fetch_all("SELECT rowid FROM source_chunks_fts WHERE source_chunks_fts MATCH 'kestrelplum'")
        assert store._fetch_all("SELECT id FROM event_log WHERE payload_json LIKE '%kestrelplum%'")
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS','1')
    context=MCPRuntimeContext(database_url=sqlite_url_for_path(db),user_id=USER_ID)
    for name,arguments in (('alice_recall',{'query':'kestrelplum'}),
                           ('alice_context_pack',{'query':'kestrelplum'}),('alice_open_loops',{})):
        blob=json.dumps(call_mcp_tool(context,name=name,arguments=arguments)).casefold()
        assert 'bob smith' not in blob and TEXT.casefold() not in blob,name
    brief=compile_local_session_brief(db,user_id=USER_ID,query='lantern',project_view=ProjectView.unscoped(),
                                     exclude_global_domains=frozenset())
    assert 'Bob Smith' not in brief and 'kestrelplum' not in brief
    assert command(db,'list','--all') == 0
    assert json.loads(capsys.readouterr().out)['sources'] == []
    stream=io.StringIO();_write_export(stream,db_path=db,user_id=UUID(USER_ID))
    for line in stream.getvalue().splitlines():
        record=json.loads(line)
        if record['record_type'] not in {'event','header','footer'}:
            assert 'kestrelplum' not in line.casefold() and 'bob smith' not in line.casefold(),line
    (folder/'another.md').write_text('Bob Smith now carries a copper lantern.')
    assert run_import(db,folder/'another.md').imported_count == 1
    assert _read(db,"SELECT name FROM vnext_entities WHERE deleted_at IS NULL") == [('Bob Smith',)]
    assert not _read(db,"SELECT id FROM event_log WHERE event_type='entity.extraction_failed'")


def test_without_yes_delete_and_prune_leave_every_table_and_sidecar_unchanged(tmp_path,capsys):
    db=_vault(tmp_path);folder=_folder(tmp_path,note=TEXT)
    sid,_=seed(db,folder)
    before=_vault_state(db);sidecar=sleep_proposals_path(db).read_bytes()
    assert command(db,'delete',sid) == 2
    preview=json.loads(capsys.readouterr().out)
    assert preview['requires_yes'] and preview['would_delete'][0]['id'] == sid
    assert preview['would_delete'][0]['chunks'] > 0
    assert _vault_state(db) == before and sleep_proposals_path(db).read_bytes() == sidecar
    (folder/'note.md').write_text('The replacement cobalt statement.')
    run_import(db,folder,supersede=True)
    before=_vault_state(db)
    assert command(db,'prune','--superseded') == 2
    assert json.loads(capsys.readouterr().out)['would_delete'][0]['id'] == sid
    assert _vault_state(db) == before
    assert command(db,'delete',sid,'--yes') == 0
    capsys.readouterr()
    assert command(db,'delete',sid,'--yes') == 1
    assert command(db,'delete',str(uuid4()),'--yes') == 1


def test_prune_age_never_touches_live_and_cannot_repeat(tmp_path,capsys):
    db=_vault(tmp_path);folder=_folder(tmp_path,note='The first cobalt version.')
    first=run_import(db,folder).source_ids[0]
    (folder/'note.md').write_text('The second cobalt version.')
    second=run_import(db,folder,supersede=True).source_ids[0]
    (folder/'note.md').write_text('The third cobalt version.')
    third=run_import(db,folder,supersede=True).source_ids[0]
    with sqlite_user_connection(db,USER_ID) as conn:
        conn.execute("UPDATE sources SET metadata_json=json_set(metadata_json,'$.superseded_at','2001-01-01T00:00:00Z') WHERE id=?",(first,))
    assert command(db,'prune','--superseded','--older-than','30','--yes') == 0
    receipt=json.loads(capsys.readouterr().out)
    assert [row['id'] for row in receipt['deleted']] == [first]
    assert live(db)[0][0] == third
    assert command(db,'prune','--superseded','--yes') == 0
    receipt=json.loads(capsys.readouterr().out)
    assert [row['id'] for row in receipt['deleted']] == [second]
    assert command(db,'prune','--superseded','--yes') == 0
    assert json.loads(capsys.readouterr().out)['deleted_count'] == 0
    assert len(_read(db,'SELECT id FROM sources')) == 3
    assert command(db,'prune','--superseded','--older-than','-1','--yes') == 1


def test_inventory_is_read_only_safe_and_filterable(tmp_path,capsys):
    db=_vault(tmp_path);folder=_folder(tmp_path,note='First cobalt statement.')
    first=run_import(db,folder).source_ids[0]
    (folder/'note.md').write_text('Second cobalt statement.')
    run_import(db,folder,supersede=True)
    with sqlite_user_connection(db,USER_ID) as conn:
        conn.execute("UPDATE sources SET title=?, external_id=?, metadata_json=json_remove(metadata_json,'$.relative_path')",
                     ('title\n\x1b[31m','/private/source/note.md'))
    before=_vault_state(db)
    assert command(db,'list','--all') == 0
    text=capsys.readouterr().out;rows=json.loads(text)['sources']
    assert len(rows)==2 and all(row['chunk_count'] > 0 for row in rows)
    assert {row['state'] for row in rows} == {'live','replaced'}
    assert '/private/' not in text and '\x1b' not in text
    assert all(row['title'] == r'title\u000a\u001b[31m' and row['path'] == 'note.md' for row in rows)
    assert _vault_state(db)==before
    assert command(db,'list','--superseded','--query',first,'--limit','1') == 0
    assert json.loads(capsys.readouterr().out)['sources'][0]['id'] == first
    assert command(db,'list','--limit','0') == 1


def test_scrub_failure_rolls_back_and_names_pending_candidates(tmp_path,monkeypatch,capsys):
    db=_vault(tmp_path);folder=_folder(tmp_path,note=TEXT)
    sid,mid=seed(db,folder);before=_vault_state(db)
    monkeypatch.setattr(SQLiteVNextStore,'redact_memory_bundle',lambda *args,**kwargs: {})
    assert command(db,'delete',sid,'--yes') == 1
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['error']['code'] == 'candidate_scrub_refused'
    assert receipt['error']['candidate_ids'] == [mid]
    assert _vault_state(db) == before
    # Proposals removed before a failed database transaction can regenerate.
    assert not sleep_proposals_path(db).read_text()


def test_doctor_quarantine_and_cli_surface(tmp_path,monkeypatch):
    from alicebot_api.mcp.registry import list_mcp_tools
    from alicebot_api.vnext_store import PostgresVNextStore
    from alicebot_api.vault_doctor import compile_local_vault_doctor
    assert hasattr(SQLiteVNextStore,'scrub_source') and not hasattr(SQLiteVNextStore,'delete_source')
    assert 'alice-memory sources delete' in _flagged_source_remedy(SQLiteVNextStore)
    assert 'DELETE /v0/vnext/sources/{id}' in _flagged_source_remedy(PostgresVNextStore)
    assert 'sources' in _KNOWN_COMMANDS
    assert _quarantine_removal_line('sources','example-id') == 'alice-memory sources delete example-id'
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS','1')
    assert not any('source' in row['name'] and any(word in row['name'] for word in ('delete','scrub','prune'))
                   for row in list_mcp_tools())
    api=Path(__file__).parents[2]/'apps/api/src/alicebot_api'
    for directory in ('mcp','routers'):
        for path in (api/directory).rglob('*.py'):
            assert 'scrub_source(' not in path.read_text(),path.name
    for path in api.glob('host_install*.py'):
        assert 'sources delete' not in path.read_text()
    db=_vault(tmp_path);folder=_folder(tmp_path,note='First cobalt.')
    run_import(db,folder)
    assert 'superseded sources:' not in compile_local_vault_doctor(db,user_id=USER_ID)
    (folder/'note.md').write_text('Second cobalt.')
    run_import(db,folder,supersede=True)
    assert 'superseded sources: 1' in compile_local_vault_doctor(db,user_id=USER_ID)


def test_committed_memory_stays_and_explain_remains_fail_closed(tmp_path,capsys,monkeypatch):
    from alicebot_api.mcp import evidence_artifacts
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from tests.unit.test_saved_provenance_reader import _identity
    db=_vault(tmp_path);folder=_folder(tmp_path,note=TEXT)
    sid=run_import(db,folder).source_ids[0]
    with sqlite_user_connection(db,USER_ID) as conn:
        store=SQLiteVNextStore(conn,USER_ID)
        memory=store.create_memory({'memory_key':'retained.fact','value':{'text':'Approved independent statement'},
            'canonical_text':'Approved independent statement','status':'active','memory_type':'semantic'})
        mid=str(memory['id'])
        store.create_provenance_link({'target_type':'memory','target_id':mid,'source_id':sid,'quote':TEXT})
    (folder/'note.md').write_text('The current cobalt statement.')
    run_import(db,folder,supersede=True)
    context=MCPRuntimeContext(database_url=sqlite_url_for_path(db),user_id=USER_ID)
    owner=evidence_artifacts._handle_alice_vnext_memory_audit(context,{'memory_id':mid})
    assert 'Approved independent statement' in json.dumps(owner)
    from alicebot_api.mcp.types import MCPToolError
    with monkeypatch.context() as scoped:
        scoped.setattr(evidence_artifacts, '_agent_identity_from_arguments', lambda *args: _identity(project=None))
        with pytest.raises(MCPToolError, match='requested explanation is unavailable'):
            evidence_artifacts._handle_alice_vnext_memory_audit(context, {'memory_id':mid})
    with sqlite_user_connection(db,USER_ID) as conn:
        store=SQLiteVNextStore(conn,USER_ID)
        links=store.list_provenance_links(target_type='memory',target_id=mid)
        assert evidence_artifacts._authorize_memory_audit_provenance(store,identity=None,
            provenance_links=links,copied_source_ids=set()) == set()
        with pytest.raises(evidence_artifacts._ExplainAuthorizationError,match='requested explanation is unavailable'):
            evidence_artifacts._authorize_memory_audit_provenance(store,identity=_identity(project=None),
                provenance_links=links,copied_source_ids=set())
    assert command(db,'delete',sid,'--yes') == 0
    receipt=json.loads(capsys.readouterr().out)
    assert receipt['deleted'][0]['memories_citing_replaced'] == [mid]
    with sqlite_user_connection(db,USER_ID) as conn:
        store=SQLiteVNextStore(conn,USER_ID)
        assert store.get_memory(mid)['canonical_text'] == 'Approved independent statement'
        assert store.list_provenance_links(target_type='memory',target_id=mid)[0]['quote'] == REMOVAL_MARKER
