"""Compare full owner/admin read responses against another checkout.

Usage: PYTHONPATH=apps/api/src:workers:. python scripts/check_unrestricted_read_parity.py --baseline /path/to/checkout
Uses synthetic SQLite copies, real admin keys and both context-pack entry points.
Only UUIDs, wall-clock timestamps and measured durations are normalized.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
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
        for mode in ('owner', 'declared_admin', 'key_admin'):
            path = Path(database + ('.keyless' if mode != 'key_admin' else ''))
            identity = {'agent_id': 'admin', 'permission_profile': 'admin_agent'} if mode == 'declared_admin' else {}
            if mode == 'key_admin':
                patch.setenv('ALICE_AGENT_API_KEY', raw)
            else:
                patch.delenv('ALICE_AGENT_API_KEY', raising=False)
            context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
            for name in ('alice_recall', 'alice_context_pack'):
                result[mode + '/' + name] = call_mcp_tool(context, name=name, arguments={
                    'query': 'Meridian Cedar Briar', 'debug': True, 'sensitivity_allowed': list(ALL_SENSITIVITY), **identity})
            @contextmanager
            def connection(_url, user_id):
                with sqlite_user_connection(path, USER) as conn:
                    yield conn
            patch.setattr(router, 'get_settings', lambda: Settings(database_url='postgresql://db'))
            patch.setattr(router, 'user_connection', connection)
            patch.setattr(router, 'PostgresVNextStore', lambda conn: SQLiteVNextStore(conn, USER))
            response = router.create_vnext_context_pack(router.VNextContextPackRequest(user_id=UUID(USER),
                query='Meridian Cedar Briar', options={'sensitivity_allowed': list(ALL_SENSITIVITY)}, **identity),
                authorization=f'Bearer {raw}' if mode == 'key_admin' else None)
            assert response.status_code == 201, response.body
            result[mode + '/http'] = json.loads(response.body)
    Path(output).write_text(json.dumps(normalize(result), sort_keys=True, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--worker', nargs=3)
    args = parser.parse_args()
    if args.worker:
        worker(*args.worker)
        return
    assert args.baseline
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key
    from tests.unit.per_project_s2_support import add_memory
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        seed = root / 'seed.sqlite3'
        bootstrap_database(seed, user_id=USER, user_email='fixture@example.invalid')
        with sqlite_user_connection(seed, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            for name, domain, sensitivity in [('Meridian', 'project', 'public'), ('Cedar', 'health', 'private'), ('Briar', 'financial', 'confidential')]:
                row = add_memory(store, key=name, text=name + ' observation', domain=domain, sensitivity=sensitivity)
                entity = store.create_entity({'name': name, 'entity_type': 'person', 'mention_count': 999})
                store.create_graph_edge({'from_type': 'memory', 'from_id': row['id'], 'to_type': 'entity', 'to_id': entity['id'], 'edge_type': 'mentions'})
        shutil.copyfile(seed, str(seed) + '.keyless')
        with sqlite_user_connection(seed, USER) as conn:
            _, raw = create_agent_key(SQLiteVNextStore(conn, USER), user_id=USER, agent_id='admin', permission_profile='admin_agent')
        key_file = root / 'key'
        key_file.write_text(raw)
        outputs = []
        for label, checkout in [('before', args.baseline.resolve()), ('after', Path.cwd())]:
            database = root / (label + '.sqlite3')
            shutil.copyfile(seed, database)
            shutil.copyfile(str(seed) + '.keyless', str(database) + '.keyless')
            output = root / (label + '.json')
            env = dict(os.environ, PYTHONPATH=str(checkout / 'apps/api/src') + ':' + str(checkout / 'workers') + ':' + str(checkout))
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(database), str(key_file), str(output)], cwd=checkout, env=env, check=True)
            outputs.append(output.read_text())
        if outputs[0] != outputs[1]:
            print(''.join(difflib.unified_diff(outputs[0].splitlines(True), outputs[1].splitlines(True), fromfile='before', tofile='after')))
            raise SystemExit(1)
        print('9/9 full unrestricted read responses are byte-equal after UUID, timestamp and duration normalization')


if __name__ == '__main__':
    main()
