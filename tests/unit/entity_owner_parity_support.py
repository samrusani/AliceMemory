"""Frozen full-response controls for ordinary owner and unbound-admin reads.

The first JSON fixture was recorded on main at 55b78515 with default and all
sensitivity selections. Every named entity has a readable linked row.
Only generated IDs, timestamps and measured durations are normalized.

The second fixture, for the orphan vault, was recorded on main at 881f482d with
default arguments only. Its vault also holds an entity with no link at all and
one whose only link is to a memory the default sensitivity selection hides, and
the query names both next to a name no row mentions. Re-record it with
``python tests/unit/entity_owner_parity_support.py --scenario orphans ROOT OUT``
under a main checkout's ``PYTHONPATH``. The branch lists neither of those two
entities for any caller, by design, so ``drop_documented_delta`` removes them
and the counters that count them from both sides before the comparison.
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


MODES = ('owner', 'declared_admin', 'key_admin')
SENSITIVITIES = ('default', 'all')


def worker(database, key_file, output, *, query='Meridian Cedar Briar', modes=MODES, sensitivities=SENSITIVITIES):
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
        for mode, sensitivity in __import__('itertools').product(modes, sensitivities):
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
                    'query': query, 'debug': True, **options, **identity})
            @contextmanager
            def connection(_url, user_id):
                with sqlite_user_connection(path, USER) as conn:
                    yield conn
            patch.setattr(router, 'get_settings', lambda: Settings(database_url='postgresql://db'))
            patch.setattr(router, 'user_connection', connection)
            patch.setattr(router, 'PostgresVNextStore', lambda conn: SQLiteVNextStore(conn, USER))
            response = router.create_vnext_context_pack(router.VNextContextPackRequest(user_id=UUID(USER),
                query=query, options=options, **identity),
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


ORPHAN_QUERY = 'Did Marcus Chen, Elena Voss and Tobias Wren meet Meridian?'
# Marcus Chen has no link at all. Elena Voss is linked only to a confidential memory, which the default sensitivity
# selection does not return even to the owner. Tobias Wren is in no row, so the grounding block is live.
UNADMITTED_ENTITIES = ('Marcus Chen', 'Elena Voss')
ORPHAN_MODES = ('owner', 'key_admin')


def seed_orphans(root: Path):
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key
    from tests.unit.per_project_s2_support import add_memory
    seed = root / 'seed.sqlite3'
    bootstrap_database(seed, user_id=USER, user_email='fixture@example.invalid')
    with sqlite_user_connection(seed, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        readable = add_memory(store, key='Meridian', text='Meridian observation', sensitivity='public')
        guarded = add_memory(store, key='Guarded', text='A guarded observation', sensitivity='confidential')
        for name, count, memory in (('Meridian', 31, readable), ('Marcus Chen', 7, None), ('Elena Voss', 5, guarded)):
            entity = store.create_entity({'name': name, 'entity_type': 'person', 'mention_count': count})
            if memory is not None:
                store.create_graph_edge({'from_type': 'memory', 'from_id': memory['id'], 'to_type': 'entity',
                                         'to_id': entity['id'], 'edge_type': 'mentions'})
    shutil.copyfile(seed, str(seed) + '.keyless')
    with sqlite_user_connection(seed, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        _, raw = create_agent_key(store, user_id=USER, agent_id='admin', permission_profile='admin_agent')
        _, reader = create_agent_key(store, user_id=USER, agent_id='reader', permission_profile='read_only_agent')
    key_file = root / 'key'
    key_file.write_text(raw)
    (root / 'reader_key').write_text(reader)
    return seed, key_file


def record_orphans(root: Path, output: Path):
    root.mkdir(parents=True, exist_ok=True)
    database, key_file = seed_orphans(root)
    worker(str(database), str(key_file), str(output), query=ORPHAN_QUERY, modes=ORPHAN_MODES, sensitivities=('default',))


def reader_grounding(root: Path):
    """The grounding block a read-only key gets from the orphan vault, as the fenced contrast."""
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    import pytest
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('ALICE_PROJECT_SCOPING', 'off')
        patch.setenv('ALICE_MCP_FULL_TOOLS', '1')
        patch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
        patch.setenv('ALICE_AGENT_API_KEY', (root / 'reader_key').read_text())
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(root / 'seed.sqlite3'), user_id=UUID(USER))
        pack = call_mcp_tool(context, name='alice_context_pack', arguments={'query': ORPHAN_QUERY, 'debug': True})
    return pack.get('grounding')


COUNTER_BLOCKS = ('budget', 'token_report')


def drop_documented_delta(value, priced=False):
    """Remove what this branch changes by design for every caller, and nothing else.

    A name needs a readable linked row to be listed, so the rows of ``UNADMITTED_ENTITIES`` leave every entity list,
    and the token counters inside the ``COUNTER_BLOCKS`` that price those rows change with them. Everything else,
    grounding included, must match. A float is compared to 12 significant digits: full-text scores are computed by
    the SQLite build, and the macOS and Linux builds can differ in the last digit of a very small score.
    """
    if isinstance(value, float):
        return float(f"{value:.12g}")
    if isinstance(value, dict):
        return {k: drop_documented_delta(v, priced or k in COUNTER_BLOCKS) for k, v in value.items()
                if not (priced and (k.endswith('token_estimate') or (k == 'entities' and isinstance(v, int))))}
    if isinstance(value, list):
        return [drop_documented_delta(v, priced) for v in value
                if not (isinstance(v, dict) and v.get('name') in UNADMITTED_ENTITIES and 'entity_type' in v)]
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("parity", "orphans"), default="parity")
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    (record_orphans if args.scenario == "orphans" else record)(args.root, args.output)
