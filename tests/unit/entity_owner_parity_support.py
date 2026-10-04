"""Frozen full-response controls for ordinary owner and unbound-admin reads.

The JSON fixture was recorded on main at 55b78515 with default and all
sensitivity selections. Every named entity has a readable linked row.
Only generated IDs, timestamps and measured durations are normalized.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import sys
from uuid import UUID

USER = '00000000-0000-0000-0000-000000000001'


def normalize(value, key=''):
    if key in ('duration_ms', 'elapsed_ms', 'latency_ms'):
        return '<duration>'
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<uuid>', value)
        return re.sub(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?', '<timestamp>', value)
    return value


def worker(database, key_file, output):
    from alicebot_api.config import Settings
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from alicebot_api.routers import vnext_retrieval as router
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
    import pytest
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 10, 4, 12, tzinfo=UTC)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    result = {}
    raw = Path(key_file).read_text()
    with pytest.MonkeyPatch.context() as patch:
        for name, module in list(sys.modules.items()):
            if name.startswith('alicebot_api') and getattr(module, 'datetime', None) is datetime:
                patch.setattr(module, 'datetime', FrozenDateTime)
        patch.setenv('ALICE_PROJECT_SCOPING', 'off')
        patch.setenv('ALICE_MCP_FULL_TOOLS', '1')
        patch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
        for mode, sensitivity in __import__('itertools').product(('owner', 'declared_admin', 'key_admin'), ('default', 'all')):
            path = Path(database + ('.keyless' if mode != 'key_admin' else ''))
            identity = {'agent_id': 'admin', 'permission_profile': 'admin_agent'} if mode == 'declared_admin' else {}
            if mode == 'key_admin':
                patch.setenv('ALICE_AGENT_API_KEY', raw)
            else:
                patch.delenv('ALICE_AGENT_API_KEY', raising=False)
            context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
            options = {'sensitivity_allowed': list(ALL_SENSITIVITY)} if sensitivity == 'all' else {}
            for name in ('alice_recall', 'alice_context_pack'):
                result[mode + '/' + sensitivity + '/' + name] = call_mcp_tool(context, name=name, arguments={
                    'query': 'Meridian Cedar Briar', 'debug': True, **options, **identity})
            @contextmanager
            def connection(_url, user_id):
                with sqlite_user_connection(path, USER) as conn:
                    yield conn
            patch.setattr(router, 'get_settings', lambda: Settings(database_url='postgresql://db'))
            patch.setattr(router, 'user_connection', connection)
            patch.setattr(router, 'PostgresVNextStore', lambda conn: SQLiteVNextStore(conn, USER))
            response = router.create_vnext_context_pack(router.VNextContextPackRequest(user_id=UUID(USER),
                query='Meridian Cedar Briar', options=options, **identity),
                authorization=f'Bearer {raw}' if mode == 'key_admin' else None)
            assert response.status_code == 201, response.body
            result[mode + '/' + sensitivity + '/http'] = json.loads(response.body)
    Path(output).write_text(json.dumps(normalize(result), sort_keys=True, indent=2) + '\n')


def seed(root: Path):
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key
    from tests.unit.per_project_s2_support import add_memory
    seed = root / 'seed.sqlite3'
    bootstrap_database(seed, user_id=USER, user_email='fixture@example.invalid')
    with sqlite_user_connection(seed, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for name, domain, sensitivity in [('Meridian', 'project', 'public'), ('Cedar', 'health', 'private'), ('Briar', 'financial', 'private')]:
            row = add_memory(store, key=name, text=name + ' observation', domain=domain, sensitivity=sensitivity)
            entity = store.create_entity({'name': name, 'entity_type': 'person', 'mention_count': 999})
            store.create_graph_edge({'from_type': 'memory', 'from_id': row['id'], 'to_type': 'entity', 'to_id': entity['id'], 'edge_type': 'mentions'})
    shutil.copyfile(seed, str(seed) + '.keyless')
    with sqlite_user_connection(seed, USER) as conn:
        _, raw = create_agent_key(SQLiteVNextStore(conn, USER), user_id=USER, agent_id='admin', permission_profile='admin_agent')
    key_file = root / 'key'
    key_file.write_text(raw)
    return seed, key_file


def record(root: Path, output: Path):
    root.mkdir(parents=True, exist_ok=True)
    database, key_file = seed(root)
    worker(str(database), str(key_file), str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    record(args.root, args.output)
