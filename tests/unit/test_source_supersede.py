"""TS3-TS16, TS24, TS26: source replacement through the real SQLite importer."""
from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from uuid import UUID

import pytest

from alicebot_api.onramp import _export_schema, _write_export, main as cli, sqlite_url_for_path
from alicebot_api.source_supersede import SupersedePolicy, classification_refusal, printed_source_label
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import count_sleep_proposals, sleep_proposals_path
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService, VNextCaptureValidationError
from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault, _folder, _read
from tests.unit.test_per_project_no_schema_change import (
    schema_digest, EXPECTED_TABLES, EXPECTED_INDEXES, EXPECTED_SCHEMA_DIGEST, _vault_state,
)


def run_import(db, folder, **kwargs):
    with sqlite_user_connection(db, USER_ID) as conn:
        return VNextCaptureService(SQLiteVNextStore(conn, USER_ID)).import_markdown_folder(folder, **kwargs)


def live(db):
    return _read(db, 'SELECT id, title, domain, sensitivity FROM sources WHERE deleted_at IS NULL ORDER BY id')


def test_replace_and_unchanged_events(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='The cobalt door opens on Monday.')
    first = run_import(db, folder)
    start = len(_read(db, 'SELECT id FROM event_log'))
    same = run_import(db, folder, supersede=True)
    assert same.status == 'duplicate' and same.to_record().get('superseded_count') is None
    assert [row[0] for row in _read(db, 'SELECT event_type FROM event_log ORDER BY rowid')[start:]] == [
        'source.duplicate_skipped', 'source.batch_import_completed']
    (folder/'note.md').write_text('The cobalt door opens on Friday.')
    new = run_import(db, folder, supersede=True)
    assert new.status == 'ok' and new.imported_count == 1
    assert new.to_record()['superseded_count'] == 1
    assert len(live(db)) == 1 and live(db)[0][0] == new.source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        old = store.get_sources_by_ids(first.source_ids, include_deleted=True)[0]
        assert old['metadata_json']['superseded_by'] == new.source_ids[0]
        assert old['metadata_json']['raw_text'] == 'The cobalt door opens on Monday.'
        assert store.get_source(first.source_ids[0]) is None
    assert run_import(db, folder).to_record().get('superseded') is None


@pytest.mark.parametrize('before,after,allowed', [
    (('unknown','internal'), ('project','internal'), True),
    (('personal','internal'), ('personal','private'), True),
    (('personal','internal'), ('personal','unknown'), True),
    (('personal','private'), ('personal','internal'), False),
    (('personal','internal'), ('unknown','internal'), False),
    (('personal','internal'), ('project','internal'), False),
    (('health','private'), ('personal','private'), False),
])
@pytest.mark.parametrize('override', [False, True])
def test_classification_matrix(tmp_path, before, after, allowed, override):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='The cobalt room holds the first plan.')
    run_import(db, folder, domain=before[0], sensitivity=before[1])
    baseline = live(db)
    (folder/'note.md').write_text('The cobalt room holds the revised plan.')
    result = run_import(db, folder, supersede=True, domain=after[0], sensitivity=after[1],
                        allow_looser_classification=override)
    if allowed or override:
        assert result.to_record()['superseded_count'] == 1
        assert live(db)[0][2:] == after
    else:
        assert result.to_record()['refused'] == [{'file':'note.md', 'reason':'looser_classification'}]
        assert live(db) == baseline


def test_omitted_labels_and_strictest_match(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='The first cobalt detail.')
    run_import(db, folder, domain='personal', sensitivity='private')
    (folder/'note.md').write_text('The second cobalt detail.')
    bare = run_import(db, folder, supersede=True)
    assert bare.to_record()['superseded_count'] == 1
    assert live(db)[0][2:] == ('personal','private')
    # Two old versions with different labels exist in pre-replacement vaults.
    (folder/'note.md').write_text('The third cobalt detail.')
    run_import(db, folder, domain='personal', sensitivity='internal')
    (folder/'note.md').write_text('The fourth cobalt detail.')
    refusal = run_import(db, folder, supersede=True, domain='personal', sensitivity='internal')
    assert refusal.refused and len(live(db)) == 2
    replaced = run_import(db, folder, supersede=True, domain='personal', sensitivity='private')
    assert replaced.to_record()['superseded_count'] == 2 and len(live(db)) == 1


@pytest.mark.parametrize('override', [False, True])
def test_scope_change_is_never_allowed(tmp_path, override):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='The first global detail.')
    run_import(db, folder)
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        conn.execute("UPDATE sources SET metadata_json=json_set(metadata_json, '$.project_scope', json('[\"project-a\"]'))")
    (folder/'note.md').write_text('The revised global detail.')
    result = run_import(db, folder, supersede=True, allow_looser_classification=override)
    assert result.refused[0]['reason'] == 'different_project_scope' and len(live(db)) == 1


def test_policy_uses_canonical_classification_objects(monkeypatch):
    from alicebot_api import vnext_brain, vnext_memory_commit, source_supersede
    assert source_supersede.SENSITIVITY_RANK is vnext_brain.SENSITIVITY_RANK
    monkeypatch.setitem(vnext_brain.SENSITIVITY_RANK, 'internal', 99)
    assert classification_refusal([{'domain':'personal','sensitivity':'internal'}],
        domain='personal', sensitivity='private', project_scope=()) == 'looser_classification'
    tree = ast.parse(Path(source_supersede.__file__).read_text())
    assert any(isinstance(node, ast.ImportFrom) and node.module == 'alicebot_api.vnext_memory_commit'
               and any(alias.name == 'SENSITIVE_DOMAINS' for alias in node.names) for node in ast.walk(tree))


def test_duplicates_of_other_paths_and_reverted_same_path(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, first='The cobalt door opens Monday.', second='The orange door opens Friday.')
    run_import(db, folder)
    (folder/'first.md').write_text('The orange door opens Friday.')
    result = run_import(db, folder/'first.md', supersede=True)
    assert result.to_record()['kept'] == [{'file':'first.md','reason':'matches_other_live_source'}]
    assert len(live(db)) == 2 and not result.superseded
    (folder/'first.md').write_text('The cobalt door opens Tuesday.')
    run_import(db, folder/'first.md')
    (folder/'first.md').write_text('The cobalt door opens Monday.')
    result = run_import(db, folder/'first.md', supersede=True)
    assert result.status == 'ok' and result.imported_count == 0 and len(result.superseded) == 1
    assert len(live(db)) == 2


@pytest.mark.parametrize('kind,connector,actor', [
    ('file','manual_file','system'), ('manual_text',None,'system'),
    ('telegram_message','telegram','system'), ('chatgpt_export','chatgpt_export','system'),
    ('markdown','markdown_folder','agent'),
])
def test_other_capture_doors_are_ineligible(tmp_path, kind, connector, actor):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='The revised lighthouse plan.')
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        # An imported envelope is planted directly so the test covers eligibility
        # independently of each unrelated door's own validation.
        store.create_source({'source_type':kind if kind != 'telegram_message' else 'message',
            'connector_name':connector, 'raw_path':str(folder/'note.md'), 'content_hash':'old-'+kind,
            'domain':'unknown','sensitivity':'unknown', 'metadata_json':{'generated_by':actor}})
    result = run_import(db, folder, supersede=True)
    assert result.imported_count == 1 and not result.superseded and len(live(db)) == 2


def test_resolved_paths_and_same_filenames_do_not_collide(tmp_path):
    db = _vault(tmp_path)
    a = _folder(tmp_path, README='The cobalt project.')
    b = tmp_path/'different'; b.mkdir(); (b/'README.md').write_text('The orange project.')
    run_import(db, a)
    assert not run_import(db, b, supersede=True).superseded
    moved = tmp_path/'moved'; a.rename(moved)
    (moved/'README.md').write_text('The moved cobalt project.')
    assert not run_import(db, moved, supersede=True).superseded
    link = tmp_path/'linked'; link.symlink_to(moved, target_is_directory=True)
    (moved/'README.md').write_text('The linked cobalt project revised.')
    assert len(run_import(db, link, supersede=True).superseded) == 1


def test_default_off_and_capture_without_policy_retire_nothing(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='Original cobalt statement.')
    run_import(db, folder)
    (folder/'note.md').write_text('Updated cobalt statement.')
    result = run_import(db, folder)
    assert result.to_record()['changed_files_count'] == 1
    assert '--supersede' in result.to_record()['replacement_hint']
    with sqlite_user_connection(db, USER_ID) as conn:
        capture = VNextCaptureService(SQLiteVNextStore(conn, USER_ID))
        capture.capture_source(SourceCaptureInput(source_type='markdown', connector_name='markdown_folder',
            raw_path=str(folder/'note.md'), raw_text='Third cobalt statement.'))
    assert len(live(db)) == 3
    api = Path(__file__).parents[2]/'apps/api/src'
    constructors = []
    for file in api.rglob('*.py'):
        tree = ast.parse(file.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef,ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (isinstance(node,ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value,ast.Name) and node.func.value.id == 'SupersedePolicy'
                    and node.func.attr == 'by_path'):
                    constructors.append((file.name,func.name))
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id == 'SupersedePolicy':
                    assert not node.args and not node.keywords, 'Non-off direct construction outside the policy factory'
    assert constructors == [('vnext_capture.py','import_markdown_folder')]


def test_unsupported_backend_refuses_before_capture():
    from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore
    service = VNextCaptureService(InMemoryVNextCaptureStore())
    with pytest.raises(VNextCaptureValidationError, match='^Source replacement is supported only by the SQLite Markdown importer$'):
        service.capture_source(SourceCaptureInput(source_type='markdown',raw_text='A statement.',
                               supersede=SupersedePolicy.by_path()))
    from alicebot_api.cli import build_parser
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['vnext','sources','import-markdown','notes','--supersede'])


def test_dry_run_and_replacement_failure_roll_back_every_table(tmp_path, monkeypatch):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='Original cobalt statement.')
    first = run_import(db, folder)
    sidecar = sleep_proposals_path(db)
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':first.source_ids[0],'excerpt':'Original'})+'\n')
    before = _vault_state(db); sidecar_before = sidecar.read_bytes()
    (folder/'note.md').write_text('Changed cobalt statement.')
    result = run_import(db, folder, supersede=True, dry_run=True)
    assert result.dry_run and len(result.superseded) == 1
    assert _vault_state(db) == before and sidecar.read_bytes() == sidecar_before
    original = SQLiteVNextStore.supersede_source
    def fail_after_retirement(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError('injected failure')
    monkeypatch.setattr(SQLiteVNextStore,'supersede_source',fail_after_retirement)
    result = run_import(db, folder, supersede=True)
    assert result.failed_count == 1 and len(live(db)) == 1
    after = _vault_state(db)
    assert all(after[1][table] == digest for table,digest in before[1].items() if table != 'event_log')


def test_retirement_derived_state_and_committed_memory(tmp_path):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='Alice Marlow carries the cobalt lantern.')
    first = run_import(db, folder)
    sid = first.source_ids[0]
    sidecar = sleep_proposals_path(db)
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':sid,'excerpt':'cobalt'})+'\n')
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        store.create_open_loop({'title':'Ask Alice Marlow','description':'cobalt note','source_id':sid,'status':'open'})
        ids = []
        for status in ('candidate','active'):
            memory = store.create_memory({'memory_key':'source-test.'+status,'value':{'text':'cobalt memory'},
                'canonical_text':'cobalt memory','status':status,'memory_type':'semantic','domain':'unknown',
                'sensitivity':'unknown'})
            ids.append(memory['id'])
            store.create_provenance_link({'target_type':'memory','target_id':memory['id'],'source_id':sid,'quote':'cobalt'})
    (folder/'note.md').write_text('The orange lantern is stored downstairs.')
    result = run_import(db, folder, supersede=True)
    assert result.failed_count == 0 and result.memories_citing_replaced == (ids[1],)
    assert count_sleep_proposals(sidecar,user_id=USER_ID) == 0
    assert sidecar.stat().st_mode & 0o777 == 0o600
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert store.get_memory(ids[0])['status'] == 'rejected'
        assert store.get_memory(ids[1])['canonical_text'] == 'cobalt memory'
        assert store.list_provenance_links(target_type='memory',target_id=ids[1])[0]['source_id'] == sid
        assert all(row['valid_to'] and not row['explanation'] and row['metadata_json'] == {}
                   for row in store.list_edges(from_id=sid))
        rows = conn.execute('SELECT name, normalized_name, mention_count, deleted_at FROM vnext_entities').fetchall()
        assert rows and all(row['mention_count'] == 0 and row['deleted_at'] and row['normalized_name'].startswith('removed:') for row in rows)
        loop = conn.execute('SELECT title,description,status FROM open_loops').fetchone()
        assert tuple(loop.values()) == ('[removed by the owner]','[removed by the owner]','dismissed')
    # The same name can be linked again after the orphan's identity is released.
    (folder/'note.md').write_text('Alice Marlow now carries the orange lantern.')
    assert run_import(db, folder, supersede=True).failed_count == 0
    assert _read(db,"SELECT count(*) FROM vnext_entities WHERE deleted_at IS NULL")[0][0] > 0
    assert not _read(db,"SELECT id FROM event_log WHERE event_type='entity.extraction_failed'")


def test_labels_and_schema_pin(tmp_path):
    assert printed_source_label('note\n\x1b[31m') == r'note\u000a\u001b[31m'
    assert printed_source_label('/private/source/note.md') == 'note.md'
    db = _vault(tmp_path)
    folder = _folder(tmp_path, note='First cobalt statement.')
    run_import(db, folder)
    before = _export_schema()
    (folder/'note.md').write_text('Second cobalt statement.')
    run_import(db, folder, supersede=True)
    with sqlite3.connect(db) as conn:
        assert schema_digest(conn) == (EXPECTED_TABLES,EXPECTED_INDEXES,EXPECTED_SCHEMA_DIGEST)
    assert _export_schema() == before


def test_import_switch_defaults(tmp_path, capsys):
    from alicebot_api.onramp import build_parser
    parser = build_parser()
    args = ['import-markdown','--from','notes']
    assert not parser.parse_args(args).supersede
    assert not parser.parse_args(args+['--no-supersede']).supersede
    assert parser.parse_args(args+['--supersede']).supersede
    assert parser.parse_args(args+['--dry-run']).dry_run
    with pytest.raises(SystemExit):
        parser.parse_args(args+['--supersede','--no-supersede'])


def test_cli_dry_run_never_creates_a_vault(tmp_path, capsys):
    target = tmp_path/'missing-vault'
    folder = _folder(tmp_path, note='A cobalt preview.')
    assert cli(['import-markdown','--from',str(folder),'--data-dir',str(target),'--dry-run','--supersede']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['dry_run'] and receipt['imported_count'] == 1
    assert not target.exists()


def semantic(value):
    import re
    if isinstance(value, dict):
        return {key:semantic(child) for key,child in value.items()
                if key not in {'retrieval_run_id','trace_id','run_id','duration_ms','elapsed_ms'}}
    if isinstance(value, list):
        return [semantic(item) for item in value]
    if isinstance(value,str):
        value = re.sub(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', '<id>',value)
        return re.sub(r'\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-][0-9:]+)?','<time>',value)
    return value


def test_replace_equivalence_to_fresh_vault_through_readers(tmp_path, monkeypatch):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.project_view import ProjectView
    from alicebot_api.session_briefing import compile_local_session_brief
    from alicebot_api.vault_doctor import compile_local_vault_doctor
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS','1')
    monkeypatch.setenv('ALICE_SEARCH_QUALITY','off')
    monkeypatch.setenv('ALICE_PROJECT_SCOPING','off')
    replaced = _vault(tmp_path/'replaced')
    fresh = _vault(tmp_path/'fresh')
    folder = _folder(tmp_path,note='Alice Marlow and Rowan Vale will check the cobalt lantern Monday.')
    old = run_import(replaced,folder).source_ids[0]
    sidecar=sleep_proposals_path(replaced)
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':old,'excerpt':'Monday'})+'\n')
    with sqlite_user_connection(replaced,USER_ID) as conn:
        SQLiteVNextStore(conn,USER_ID).create_open_loop({'title':'Ask Rowan Vale','source_id':old,'status':'open'})
    (folder/'note.md').write_text('Alice Marlow will check the cobalt lantern Friday.')
    assert run_import(replaced,folder,supersede=True).superseded
    run_import(fresh,folder)
    def surfaces(db):
        context=MCPRuntimeContext(database_url=sqlite_url_for_path(db),user_id=USER_ID)
        results={name:call_mcp_tool(context,name=name,arguments=arguments) for name,arguments in (
            ('alice_recall',{'query':'cobalt lantern'}),
            ('alice_context_pack',{'query':'cobalt lantern'}),
            ('alice_open_loops',{}),
        )}
        results['brief']=compile_local_session_brief(db,user_id=USER_ID,query='cobalt lantern',
                        project_view=ProjectView.unscoped(),exclude_global_domains=frozenset())
        doctor=compile_local_vault_doctor(db,user_id=USER_ID)
        results['doctor_counts']=[line for line in doctor.splitlines() if line.startswith((
            'sources:','searchable chunks:','committed facts:','candidates waiting:','sleep proposals:'))]
        results['entities']=_read(db,'SELECT name,mention_count FROM vnext_entities WHERE deleted_at IS NULL ORDER BY name')
        stream=io.StringIO();_write_export(stream,db_path=db,user_id=UUID(USER_ID))
        records=[json.loads(line) for line in stream.getvalue().splitlines()]
        results['live_source_export']=[item for item in records if item.get('record_type') in {'source','source_chunk'}]
        assert len(results['live_source_export']) >= 2
        return semantic(results)
    assert surfaces(replaced) == surfaces(fresh)


def test_sidecar_is_pruned_before_commit_and_failure_refuses_replacement(tmp_path,monkeypatch):
    import alicebot_api.vault_sleep as sleep
    db=_vault(tmp_path);folder=_folder(tmp_path,note='The original cobalt statement.')
    old=run_import(db,folder).source_ids[0];sidecar=sleep_proposals_path(db)
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':old})+'\n')
    (folder/'note.md').write_text('The updated cobalt statement.')
    rewrite=sleep._write_jsonl;seen=[]
    def assert_before_commit(path,rows):
        assert live(db)[0][0] == old
        seen.append(True);rewrite(path,rows)
    monkeypatch.setattr(sleep,'_write_jsonl',assert_before_commit)
    assert run_import(db,folder,supersede=True).superseded and seen == [True]
    newest=live(db)[0][0]
    sidecar.write_text(json.dumps({'user_id':USER_ID,'source_id':newest})+'\n')
    def fail(*args):
        raise OSError('injected write failure')
    monkeypatch.setattr(sleep,'_write_jsonl',fail)
    (folder/'note.md').write_text('The third cobalt statement.')
    assert run_import(db,folder,supersede=True).failed_count == 1
    assert live(db)[0][0] == newest


def test_downgrade_to_release_tag(tmp_path):
    repo=Path(__file__).parents[2]
    archived=subprocess.run(['git','archive','v0.20.0','apps/api/src'],cwd=repo,capture_output=True)
    if archived.returncode:
        if os.environ.get('CI'):
            pytest.fail('Required v0.20.0 tag is missing')
        pytest.skip('v0.20.0 tag is not available locally')
    release=tmp_path/'release';release.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as bundle:
        bundle.extractall(release,filter='data')
    db=_vault(tmp_path)
    folder=_folder(tmp_path,note='The old cobalt statement.')
    run_import(db,folder)
    (folder/'note.md').write_text('The current cobalt statement.')
    run_import(db,folder,supersede=True)
    old_ids=[row[0] for row in _read(db,"SELECT id FROM sources WHERE deleted_at IS NOT NULL")]
    assert len(old_ids) == 1
    spare=folder/'spare.md';spare.write_text('A spare source to scrub.')
    spare_id=run_import(db,spare).source_ids[0]
    assert cli(['sources','delete',spare_id,'--yes','--db',str(db)]) == 0
    assert cli(['sources','prune','--superseded','--yes','--db',str(db)]) == 0
    assert len(_read(db,'SELECT id FROM sources')) == 3
    assert len(live(db)) == 1
    export=tmp_path/'export.jsonl';restored=tmp_path/'restored'
    code='''
import io,json,sys
from pathlib import Path
from uuid import UUID
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import _export_schema,_write_export,main,sqlite_url_for_path
from alicebot_api.vault_doctor import compile_local_vault_doctor
path=Path(sys.argv[1]); uid=sys.argv[4]
context=MCPRuntimeContext(database_url=sqlite_url_for_path(path),user_id=uid)
answer=call_mcp_tool(context,name="alice_recall",arguments={"query":"cobalt"})
assert "current cobalt" in json.dumps(answer), answer
assert "old cobalt" not in json.dumps(answer), answer
assert "sources: 1" in compile_local_vault_doctor(path,user_id=uid)
with open(sys.argv[2],"w") as stream: _write_export(stream,db_path=path,user_id=UUID(uid))
assert main(["import","--in",sys.argv[2],"--data-dir",sys.argv[3]]) == 0
print("RELEASE_FINGERPRINT="+_export_schema()["fingerprint"])
'''
    env={key:value for key,value in os.environ.items() if not key.startswith(('ALICE_','ALICEBOT_'))}
    env['PYTHONPATH']=str(release/'apps/api/src')
    completed=subprocess.run([sys.executable,'-c',code,str(db),str(export),str(restored),USER_ID],
                             cwd=release,env=env,capture_output=True,text=True,timeout=90)
    assert completed.returncode == 0,completed.stdout+completed.stderr
    assert 'RELEASE_FINGERPRINT='+_export_schema()['fingerprint'] in completed.stdout
