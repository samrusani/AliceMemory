"""Real SQLite and real-key matrix for entity names and stored mention counts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from itertools import product
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPNotPermittedError, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import (
    ALL_SENSITIVITY, PERMISSION_PROFILES, VNEXT_DOMAINS, AgentIdentity, evaluate_agent_policy,
)
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.unit.per_project_s2_support import add_memory

USER = '00000000-0000-0000-0000-000000000001'
CALLERS = [(None, None, None)] + list(product(('key', 'declared'), PERMISSION_PROFILES, (None, 'alpha')))
LABELS = list(product(VNEXT_DOMAINS, ALL_SENSITIVITY, (None, 'alpha', 'beta')))

@pytest.fixture(scope='module')
def entity_vault(tmp_path_factory):
    path = tmp_path_factory.mktemp('entities') / 'vault.sqlite3'
    bootstrap_database(path, user_id=USER, user_email='local@alice')
    keys = {}
    with sqlite_user_connection(path, USER) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        store = SQLiteVNextStore(conn, USER)
        anchor = add_memory(store, key='anchor', text='Anchor shared context', scope=('alpha',))
        entity = store.create_entity({'name': 'Cedar', 'entity_type': 'person', 'mention_count': 999})
        memory = add_memory(store, key='case', text='Fixture observation')
        case_id = str(memory['id'])
        store.create_graph_edge({'from_type': 'memory', 'from_id': memory['id'], 'to_type': 'entity',
                                 'to_id': entity['id'], 'edge_type': 'mentions'})
        entity = store.create_entity({'name': 'Anchor', 'entity_type': 'person', 'mention_count': 1})
        store.create_graph_edge({'from_type': 'memory', 'from_id': anchor['id'], 'to_type': 'entity',
                                 'to_id': entity['id'], 'edge_type': 'mentions'})
    keyless = path.with_name('keyless.sqlite3')
    with sqlite3.connect(path) as source, sqlite3.connect(keyless) as target:
        source.backup(target)
    with sqlite_user_connection(path, USER) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        store = SQLiteVNextStore(conn, USER)
        for profile, project in product(PERMISSION_PROFILES, (None, 'alpha')):
            _, keys[profile, project] = create_agent_key(
                store, user_id=USER, agent_id=f'{profile}-{project}',
                permission_profile=profile, project_scope=project,
            )
    return path, keyless, keys, case_id


def oracle(profile, binding, domain, sensitivity, project):
    """Ask the pre-existing policy about the row, independently of graph code."""
    identity = None if profile is None else AgentIdentity(
        agent_id='reader', permission_profile=profile, project_scope=(binding,) if binding else (),
        project_scope_locked=bool(binding),
    )
    decision = evaluate_agent_policy(identity=identity, action='memory.recall', domains=(domain,),
                                     sensitivity_allowed=(sensitivity,), project_scope=(project,) if project else (),
                                     require_explicit_project_scope=True)
    return decision.decision == 'allowed'


@pytest.mark.parametrize('mode,profile,binding', CALLERS)
@pytest.mark.parametrize('door', ('recall', 'pack', 'http'))
def test_entity_matrix(entity_vault, monkeypatch, request, mode, profile, binding, door):
    path, keyless, keys, case_id = entity_vault
    if mode != "key":
        path = keyless
    # The real schema is bootstrapped once above; avoid rerunning all unrelated
    # upgrades on each of the 19,656 reads in this retrieval-only matrix.
    monkeypatch.setattr('alicebot_api.sqlite_store.bootstrap_sqlite_schema', lambda conn: conn.execute('PRAGMA synchronous=OFF'))
    monkeypatch.delenv('ALICE_AGENT_API_KEY', raising=False)
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS', '1')
    monkeypatch.setenv('ALICE_PROJECT_SCOPING', 'off')
    monkeypatch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
    # Reuse one real connection per caller. Authentication, policy, handlers
    # and SQL still execute on every call; schema opens and disk flushes are
    # outside this read-fence matrix and have their own store tests.
    opened = sqlite_user_connection(path, USER)
    shared_conn = opened.__enter__()
    request.addfinalizer(lambda: opened.__exit__(None, None, None))
    @contextmanager
    def shared_connection(_path, _user):
        yield shared_conn
    monkeypatch.setattr('alicebot_api.mcp.runtime.sqlite_user_connection', shared_connection)
    key = keys[profile, binding] if mode == 'key' else None
    if key:
        monkeypatch.setenv('ALICE_AGENT_API_KEY', key)
    identity = {} if mode != 'declared' else {'agent_id': 'reader', 'permission_profile': profile,
                                             'project_scope': [binding] if binding else []}
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(USER))
    if door == 'http':
        from alicebot_api.config import Settings
        from alicebot_api.routers import vnext_retrieval as router
        @contextmanager
        def connection(_url, user_id):
            yield shared_conn
        monkeypatch.setattr(router, 'get_settings', lambda: Settings(database_url='postgresql://db'))
        monkeypatch.setattr(router, 'user_connection', connection)
        monkeypatch.setattr(router, 'PostgresVNextStore', lambda conn: SQLiteVNextStore(conn, USER))
    @contextmanager
    def isolated_case():
        # Requests exercise real persistence, then discard their audit and
        # context-pack writes so the next matrix cell starts from the same vault.
        shared_conn.execute('SAVEPOINT matrix_case')
        try:
            yield
        finally:
            shared_conn.execute('ROLLBACK TO matrix_case')
            shared_conn.execute('RELEASE matrix_case')
    errors = []
    for index, (domain, sensitivity, project) in enumerate(LABELS):
        with isolated_case():
            # Reuse the synthetic input's ID while changing its persisted labels.
            # This keeps the complete policy cross-product small and independent.
            SQLiteVNextStore(shared_conn, USER).update_memory(memory_id=case_id, patch={
                'domain': domain, 'sensitivity': sensitivity, 'project_id': project,
                'project_scope': [project] if project else [],
                'metadata_json': {'project_scope': [project] if project else []},
            })
            query = 'Anchor Cedar' 
            if door == 'http':
                response = router.create_vnext_context_pack(router.VNextContextPackRequest(
                    user_id=UUID(USER), query=query, options={'max_items': 10, 'sensitivity_allowed': list(ALL_SENSITIVITY)},
                    **identity), authorization=f'Bearer {key}' if key else None)
                if profile == 'project_scoped_agent' and binding is None:
                    assert response.status_code == 403, response.body
                    assert b'project_scope_required' in response.body
                    continue
                assert response.status_code == 201, response.body
                payload = json.loads(response.body)
            else:
                arguments = {'query': query, 'debug': True, 'sensitivity_allowed': list(ALL_SENSITIVITY), **identity}
                if door == 'pack' and profile == 'project_scoped_agent' and binding is None:
                    with pytest.raises(MCPNotPermittedError, match='project_scope_required'):
                        call_mcp_tool(context, name='alice_context_pack', arguments=arguments)
                    continue
                payload = call_mcp_tool(context, name='alice_recall' if door == 'recall' else 'alice_context_pack', arguments=arguments)
        # Every copy in a debug trace and the outward list is checked.
        def entities(value):
            if isinstance(value, dict):
                if value.get('name') == 'Cedar' and value.get('entity_type') == 'person':
                    yield value
                for nested in value.values():
                    yield from entities(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from entities(nested)
        copies = list(entities(payload))
        allowed = oracle(profile, binding, domain, sensitivity, project)
        if copies and not allowed:
            errors.append((index, 'hidden name', domain, sensitivity, project))
        # Only the owner and an unbound admin have no read restriction here.
        unrestricted = profile in (None, 'admin_agent') and binding is None
        if copies and not unrestricted and any('mention_count' in row for row in copies):
            errors.append((index, 'stored count', domain, sensitivity, project))
        if unrestricted:
            assert copies and all(row['mention_count'] == 999 for row in copies)
        if allowed and door == 'recall':
            assert copies, (profile, binding, domain, sensitivity, project)
    print(f'entity-matrix {door} {mode} {profile} {binding}: {len(LABELS)} cases, {len(errors)} violations')
    assert not errors, errors[:8]


def _graph_fixture(*, hidden=False, count=17):
    from tests.unit.test_vnext_retrieval import InMemoryVNextRetrievalStore, _memory_row, _entity_row, _mention_edge
    memory = _memory_row('memory', 'Private observation', domain='health' if hidden else 'project')
    return InMemoryVNextRetrievalStore(memories=[memory], sources=[],
        entities=[_entity_row('entity', 'Meridian', mention_count=count)], edges=[_mention_edge('memory', 'entity')])


def _graph(store, **overrides):
    from alicebot_api.vnext_retrieval import VNextRetrievalService
    kwargs = {'query': 'Meridian', 'domains': ['project'], 'sensitivity_allowed': ['private'], 'limit': 8}
    return VNextRetrievalService(store)._memory_graph_rows(**(kwargs | overrides))


def test_hidden_entity_matches_absent_entity_including_debug_status():
    hidden = _graph_fixture(hidden=True)
    rows, status, entities = _graph(hidden)
    hidden.entities = []
    assert (rows, status, entities) == _graph(hidden)


def test_fenced_count_is_omitted_but_unrestricted_count_survives():
    store = _graph_fixture()
    assert _graph(store)[2] == [{'id': 'entity', 'name': 'Meridian', 'entity_type': 'organization'}]
    assert _graph(store, domains=[], sensitivity_allowed=list(ALL_SENSITIVITY))[2][0]['mention_count'] == 17


def test_fenced_selection_ignores_hidden_counts_and_filters_before_limit():
    from tests.unit.test_vnext_retrieval import _entity_row, _memory_row, _mention_edge
    store = _graph_fixture()
    for i in range(7):
        store.entities.append(_entity_row(f'hidden{i}', f'Hidden{i}', mention_count=1000+i))
        store.memories.append(_memory_row(f'm{i}', 'Private observation', domain='health'))
        store.edges.append(_mention_edge(f'm{i}', f'hidden{i}'))
    assert [row['name'] for row in _graph(store, query='Meridian ' + ' '.join(f'Hidden{i}' for i in range(7)))[2]] == ['Meridian']
    store.entities[0]['mention_count'] = 4000
    assert [row['name'] for row in _graph(store, query='Meridian ' + ' '.join(f'Hidden{i}' for i in range(7)))[2]] == ['Meridian']


@pytest.mark.parametrize('reverse', (False, True))
def test_source_only_entities_obey_domain_sensitivity_and_project(reverse):
    store = _graph_fixture()
    store.memories = []
    source = {'id': 'source', 'domain': 'project', 'sensitivity': 'private', 'metadata_json': {'project_scope': ['alpha']}}
    store.sources = [source]
    store.edges = [{'from_type': 'source', 'from_id': 'source', 'to_type': 'entity', 'to_id': 'entity', 'edge_type': 'mentions'}]
    if reverse:
        store.edges[0] = {'from_type': 'entity', 'from_id': 'entity', 'to_type': 'source', 'to_id': 'source', 'edge_type': 'about'}
    assert _graph(store, projects=('alpha',))[2]
    assert not _graph(store, projects=('beta',))[2]
    source['domain'] = 'health'
    assert not _graph(store, projects=('alpha',))[2]
    source['domain'] = 'project'
    source['sensitivity'] = 'confidential'
    assert not _graph(store, projects=('alpha',))[2]
    source['sensitivity'] = 'private'
    source['deleted_at'] = '2026-10-01'
    assert not _graph(store, projects=('alpha',))[2]


def test_grounding_does_not_treat_a_hidden_entity_as_corpus_support():
    from alicebot_api.vnext_grounding import corpus_support
    store = _graph_fixture(hidden=True)
    from alicebot_api.vnext_entities import _REQUIRED_STORE_METHODS
    for method in (*_REQUIRED_STORE_METHODS, 'create_graph_edge'):
        if not callable(getattr(store, method, None)):
            setattr(store, method, lambda *args, **kwargs: None)
    assert corpus_support(['Meridian'], store, domains=['project'], sensitivity_allowed=['private'], allow_entity_lookup=False) == {'Meridian': False}


def test_explain_omits_counts_when_fenced():
    from alicebot_api.mcp.evidence_artifacts import _memory_linked_entities
    store = _graph_fixture()
    store.get_entity = lambda entity_id: store.entities[0]
    assert 'mention_count' not in _memory_linked_entities(store, 'memory', include_entity_counts=False)[0]
    assert _memory_linked_entities(store, 'memory')[0]['mention_count'] == 17


def test_filtered_entity_order_does_not_follow_stored_counts():
    from tests.unit.test_vnext_retrieval import _entity_row, _mention_edge
    store = _graph_fixture()
    store.entities.append(_entity_row('other', 'Briar', mention_count=1))
    store.edges.append(_mention_edge('memory', 'other'))
    before = _graph(store, query='Meridian Briar')[2]
    store.entities[1]['mention_count'] = 1000
    assert _graph(store, query='Meridian Briar')[2] == before


def test_source_entity_obeys_an_until_only_filter():
    from datetime import UTC, datetime
    store = _graph_fixture()
    store.memories = []
    store.sources = [{'id': 'source', 'domain': 'project', 'sensitivity': 'private', 'source_created_at': '2026-10-04T00:00:00Z'}]
    store.edges = [{'from_type': 'source', 'from_id': 'source', 'to_type': 'entity', 'to_id': 'entity', 'edge_type': 'mentions'}]
    assert not _graph(store, scope_window_end=datetime(2026, 10, 1, tzinfo=UTC))[2]
    assert _graph(store, scope_window_end=datetime(2026, 10, 5, tzinfo=UTC))[2]


def test_source_only_entities_do_not_bypass_memory_specific_filters():
    store = _graph_fixture()
    store.memories = []
    store.sources = [{'id': 'source', 'domain': 'project', 'sensitivity': 'private'}]
    store.edges = [{'from_type': 'source', 'from_id': 'source', 'to_type': 'entity', 'to_id': 'entity', 'edge_type': 'mentions'}]
    for restriction in ({'memory_types': ('semantic',)}, {'created_by_agent_ids': ('reader',)},
                        {'run_id': 'run'}, {'scope_thread_id': 'thread'}, {'scope_task_id': 'task'}):
        assert not _graph(store, **restriction)[2]
    store.edges[0]['valid_to'] = '2026-10-01T00:00:00Z'
    store.list_edges = lambda **kwargs: store.edges
    assert not _graph(store)[2]


def test_explain_real_key_count_fence(entity_vault, monkeypatch):
    path, keyless, keys, memory_id = entity_vault
    monkeypatch.setenv('ALICE_PROJECT_SCOPING', 'off')
    monkeypatch.setenv('ALICE_MCP_FULL_TOOLS', '1')
    for profile in (None, 'read_only_agent', 'admin_agent'):
        if profile:
            monkeypatch.setenv('ALICE_AGENT_API_KEY', keys[profile, None])
        else:
            monkeypatch.delenv('ALICE_AGENT_API_KEY', raising=False)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(path if profile else keyless), user_id=UUID(USER))
        payload = call_mcp_tool(context, name='alice_explain', arguments={'memory_id': memory_id})
        def counts(value):
            if isinstance(value, dict):
                if value.get('name') == 'Cedar':
                    yield 'mention_count' in value
                for nested in value.values():
                    yield from counts(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from counts(nested)
        found = list(counts(payload))
        assert found and all(item == (profile != 'read_only_agent') for item in found)


def test_context_pack_passes_the_entity_fence_to_grounding(tmp_path):
    from alicebot_api.vnext_retrieval import VNextRetrievalRequest, VNextRetrievalService
    from alicebot_api.vnext_source_fence import SourceReadFence
    path = tmp_path / 'grounding.sqlite3'
    bootstrap_database(path, user_id=USER, user_email='fixture@example.invalid')
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        memory = add_memory(store, key='private', text='Private observation', domain='health')
        entity = store.create_entity({'name': 'Marcus Chen', 'entity_type': 'person'})
        store.create_graph_edge({'from_type': 'memory', 'from_id': memory['id'], 'to_type': 'entity', 'to_id': entity['id'], 'edge_type': 'mentions'})
        pack = VNextRetrievalService(store).compile_context_pack(VNextRetrievalRequest(
            query='Did Marcus Chen approve the launch?', domains=('project',)), source_fence=SourceReadFence.unfenced())
        assert pack.get('grounding', {}).get('unsupported_entities') == ['Marcus Chen']
