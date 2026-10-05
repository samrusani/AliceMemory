"""Owner-only SQLite source inventory, deletion preview and scrub commands."""
from __future__ import annotations

import json
from uuid import UUID

from alicebot_api.source_supersede import printed_source_label
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import SleepError
from alicebot_api.vnext_stores.memory_lifecycle_common import is_redacted_memory
from alicebot_api.vnext_stores.sqlite.source_retirement import (
    CandidateScrubRefused, citing_memories_by_source, optimize_scrub_indexes, source_open_loop_count)

RETAINED_DATA = (
    "Source and import events keep prior titles, hashes and import folder paths. "
    "Source hash columns and earlier backups are not erased. Unused space and the write-ahead log can retain old text. "
    "Committed memories and all other unredacted memories keep their text. Review every listed memory id; "
    "use alice_memory_manage with action forget for each memory that should leave recall, "
    "or the owner's memory redaction command to overwrite its text. "
    "For file cleanup, stop every program using the vault and follow the VACUUM and checkpoint steps in "
    "docs/integrations/importers.md#list-delete-and-prune-sqlite-sources."
)


def source_record(row):
    relative = row['metadata_json'].get('relative_path') or row.get('external_id') or row.get('raw_path')
    return {'id':str(row['id']), 'title':printed_source_label(row.get('title')),
            'type':printed_source_label(row['source_type']), 'path':printed_source_label(relative),
            'chunk_count':row.get('chunk_count',0), 'domain':row['domain'], 'sensitivity':row['sensitivity'],
            'captured_at':row['captured_at'], 'state':'replaced' if row['deleted_at'] else 'live'}


def _targets(store, args):
    if args.sources_command == 'prune':
        return store.prunable_sources(older_than=args.older_than)
    rows = store.get_sources_by_ids([str(args.source_id)],include_deleted=True)
    if not rows or rows[0]['metadata_json'].get('scrubbed'):
        raise ValueError('Source is unknown or already scrubbed')
    return rows


def _preview(store, rows):
    # One pass over the memories answers for every source of the preview.
    citing=citing_memories_by_source(store,[str(row['id']) for row in rows])
    result = []
    for row in rows:
        sid=str(row['id'])
        counts=store._fetch_one('source deletion preview',
            "SELECT (SELECT count(*) FROM source_chunks WHERE user_id=? AND source_id=?) AS chunks, "
            "(SELECT count(*) FROM provenance_links p WHERE p.user_id=? AND (p.source_id=? OR EXISTS "
            "(SELECT 1 FROM source_chunks c WHERE c.user_id=p.user_id AND c.id=p.source_chunk_id "
            "AND c.source_id=?))) AS provenance_quotes",
            (store.user_id,sid,store.user_id,sid,sid))
        # The loops the scrub blanks: the one rule of the source reverse lookup, not the column alone.
        counts['open_loops']=source_open_loop_count(store,sid)
        memories=[memory for memory in citing[sid] if not is_redacted_memory(memory)]
        counts['candidate_memories']=sum(memory['status'] in {'candidate','needs_review','rejected'} for memory in memories)
        counts['memories_citing_replaced']=[str(memory['id']) for memory in memories
            if memory['status'] not in {'candidate','needs_review','rejected'}]
        result.append({'id':sid,'title':printed_source_label(row['title']), **counts})
    return result


def run_sources(args):
    from alicebot_api.onramp import resolve_db_path, _prepared_export_connection
    db=resolve_db_path(data_dir=args.data_dir,db=args.db)
    if not db.is_file():
        print(json.dumps({'error':{'code':'vault_not_found','message':'No vault exists at the selected location'}}))
        return 1
    try:
        if args.sources_command == 'delete':
            try:
                args.source_id = UUID(str(args.source_id))
            except ValueError as exc:
                raise ValueError('Source id must be a valid UUID') from exc
        # Lists and confirmation previews use a private read-only snapshot.
        # An explicit --yes reselects under the vault's writer lock below.
        if args.sources_command == 'list' or not args.yes:
            with _prepared_export_connection(db,args.user_id) as conn:
                store=SQLiteVNextStore(conn,args.user_id)
                if args.sources_command == 'list':
                    records=store.source_inventory(query=args.query,superseded=args.superseded,
                                                   all_sources=args.all,limit=args.limit)
                    print(json.dumps({'sources':[source_record(row) for row in records]},sort_keys=True,ensure_ascii=True))
                    return 0
                preview=_preview(store,_targets(store,args))
            print(json.dumps({'would_delete':preview,'requires_yes':True,'retained_data':RETAINED_DATA},sort_keys=True))
            return 2
        with sqlite_user_connection(db,args.user_id) as conn:
            store=SQLiteVNextStore(conn,args.user_id)
            with store.savepoint():
                rows=_targets(store,args)
                # One pass over the memories finds the citing memories of every source; each scrub reads those again.
                citing=citing_memories_by_source(store,[str(row['id']) for row in rows])
                receipts=[{'id':str(row['id']),**store.scrub_source(str(row['id']), optimize=False,
                          citing_ids=[str(memory['id']) for memory in citing[str(row['id'])]])} for row in rows]
                if receipts:
                    optimize_scrub_indexes(store)
        print(json.dumps({'deleted_count':len(receipts),'deleted':receipts,'retained_data':RETAINED_DATA},sort_keys=True))
        return 0
    except CandidateScrubRefused as exc:
        print(json.dumps({'error':{'code':'candidate_scrub_refused','candidate_ids':exc.candidate_ids,
                                 'message':'Candidate memories could not be scrubbed; database changes were rolled back. '
                                           'Sleep proposals may have been removed; run alice-memory sleep to regenerate them.'}}))
        return 1
    except SleepError:
        print(json.dumps({'error':{'code':'sleep_sidecar_refused',
            'message':'Sleep proposals could not be read. Stop every program using the vault, move '
                      'sleep_proposals.jsonl aside, and retry. Run alice-memory sleep to regenerate proposals; '
                      'do not restore the moved file after deleting sources.'}}))
        return 1
    except ValueError as exc:
        print(json.dumps({'error':{'code':'source_request_refused','message':str(exc)}}))
        return 1
