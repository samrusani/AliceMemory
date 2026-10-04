"""Restricted inputs must not become an unrestricted derived memory or report."""
from __future__ import annotations

from itertools import product
from types import SimpleNamespace

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, PERMISSION_PROFILES, RESTRICTED_DOMAINS, VNEXT_DOMAINS, evaluate_agent_policy
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_brain import _artifact_domain
from alicebot_api.vnext_consolidation import _domain
from alicebot_api.vnext_rollups import _dominant_domain
from tests.unit.per_project_s2_support import add_memory

USER = '00000000-0000-0000-0000-000000000001'

@pytest.fixture(scope='module')
def domain_vault(tmp_path_factory):
    path = tmp_path_factory.mktemp('derived') / 'vault.sqlite3'
    bootstrap_database(path, user_id=USER, user_email='local@alice')
    with sqlite_user_connection(path, USER) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        store = SQLiteVNextStore(conn, USER)
        identities = [None]
        # Resolve declared profiles before there are keys, as a keyless install does.
        for profile, binding in product(PERMISSION_PROFILES, (None, 'alpha')):
            identities.append(resolve_agent_identity(store, user_id=USER, raw_key=None, payload={
                'agent_id': 'reader', 'permission_profile': profile, 'project_scope': [binding] if binding else [],
            }))
        for profile, binding in product(PERMISSION_PROFILES, (None, 'alpha')):
            _, key = create_agent_key(store, user_id=USER, agent_id=f'{profile}-{binding}',
                                      permission_profile=profile, project_scope=binding)
            identities.append(resolve_agent_identity(store, user_id=USER, raw_key=key, payload={}))
        yield store, identities


@pytest.mark.parametrize('domain,sensitivity,project', list(product(VNEXT_DOMAINS, ALL_SENSITIVITY, (None, 'alpha', 'beta'))))
def test_derived_label_matrix(domain_vault, domain, sensitivity, project):
    store, identities = domain_vault
    rows = [add_memory(store, key=f'input.{domain}.{sensitivity}.{project}', text='Cedar observation', domain=domain,
                       sensitivity=sensitivity, scope=(project,) if project else None),
            {'domain': 'project', 'sensitivity': sensitivity}]
    request = SimpleNamespace(domains=())
    labels = (_artifact_domain(request, rows), _domain(request, rows), _dominant_domain(tuple(rows)))
    expected = domain if domain in RESTRICTED_DOMAINS or domain == 'project' else 'unknown'
    assert labels == (expected,) * 3
    # Persist the production roll-up selector's result into a real SQLite row.
    derived = add_memory(store, key=f'derived.{domain}.{sensitivity}.{project}', text='Cedar summary',
                         domain=labels[2], sensitivity=sensitivity, scope=(project,) if project else None)
    for identity in identities:
        def readable(label):
            decision = evaluate_agent_policy(identity=identity, action='memory.recall', domains=(label,),
                sensitivity_allowed=(sensitivity,), project_scope=(project,) if project else (),
                require_explicit_project_scope=True)
            return decision.decision == 'allowed'
        assert not readable(str(derived['domain'])) or readable(domain), (identity, domain, sensitivity, project)


def test_restricted_majority_ties_and_explicit_request():
    rows = [{'domain': name} for name in ('project', 'health', 'legal', 'legal', 'health')]
    for selector in (_artifact_domain, _domain):
        assert selector(SimpleNamespace(domains=('project',)), rows) == 'health'
    assert _dominant_domain(tuple(rows)) == 'health'
    rows.append({'domain': 'legal'})
    assert _dominant_domain(tuple(rows)) == 'legal'
    assert _artifact_domain(SimpleNamespace(domains=('professional',)), [{'domain': 'project'}]) == 'professional'
    assert _dominant_domain(({'domain': 'project'}, {'domain': 'learning'})) == 'unknown'


def test_sqlite_upgrade_relabels_existing_derived_memory_only(tmp_path):
    from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
    path = tmp_path / 'upgrade.sqlite3'
    bootstrap_database(path, user_id=USER, user_email='local@alice')
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = add_memory(store, key='input', text='Private observation', domain='health')
        candidate = store.create_memory({'memory_key': 'rollup', 'canonical_text': 'Private summary', 'status': 'candidate',
            'domain': 'unknown', 'metadata_json': {'consolidation': {'cluster_member_ids': [source['id']]}}})
        unknown = store.create_memory({'memory_key': 'no-inputs', 'canonical_text': 'Old summary', 'status': 'candidate',
            'domain': 'unknown', 'metadata_json': {'candidate_kind': 'memory_consolidation'}})
        # Retained deleted inputs still contributed to the stored text.
        conn.execute('UPDATE memories SET deleted_at = ? WHERE id = ?', ('2026-10-01T00:00:00Z', source['id']))
        conn.execute("DELETE FROM alice_schema_state WHERE key = 'derived_restricted_domains_v1'")
        conn.commit()
        bootstrap_sqlite_schema(conn)
        after = store.get_memory(str(candidate['id']))
        assert after['domain'] == 'health'
        assert {k: v for k, v in after.items() if k != 'domain'} == {k: v for k, v in candidate.items() if k != 'domain'}
        assert store.get_memory(str(unknown['id']))['domain'] == 'unknown'
        bootstrap_sqlite_schema(conn)
        assert store.get_memory(str(candidate['id'])) == after


def test_recorded_input_shapes_chains_cycles_users_and_redaction():
    from alicebot_api.vnext_derived_domain_backfill import plan_relabels
    def row(identifier, domain='unknown', user='a', **metadata):
        return {'id': identifier, 'user_id': user, 'domain': domain, 'metadata_json': metadata}
    tables = {
        'sources': [row('source', 'health'), row('foreign', 'legal', user='b')],
        'open_loops': [row('loop', 'financial')],
        'memories': [row('input', 'spiritual'), row('weekly', discovered_by='vnext_weekly_synthesis'),
                     row('rollup', consolidation={'cluster_member_ids': ['input']})],
        'generated_artifacts': [
            row('brain', input_summary={'source_ids': ['source']}, candidate_memory_ids=['weekly']),
            row('chain', input_summary={'artifact_ids': ['brain']}),
            row('connection', source_ids=['source']),
            row('contradiction', memory_ids=['input']),
            row('stale', stale_marked_memory_ids=['input']),
            row('loops', open_loop_ids=['loop']),
            row('consolidation', consolidation={'cluster_membership': [['input']]}),
            row('rollups', rollups={'groups': [{'member_ids': ['input']}]}),
            row('refs', source_refs=['memory:input']),
            row('cycle1', input_summary={'artifact_ids': ['cycle2'], 'source_ids': ['source']}),
            row('cycle2', input_summary={'artifact_ids': ['cycle1']}),
            row('foreign-ref', source_ids=['foreign']), row('no-inputs'),
            row('redacted', redacted=True, source_ids=['source']),
        ],
    }
    updates = plan_relabels(tables)
    by_id = {item[2]: item[3] for item in updates}
    assert by_id == {'weekly': 'health', 'rollup': 'spiritual', 'brain': 'health', 'chain': 'health',
                     'connection': 'health', 'contradiction': 'spiritual', 'stale': 'spiritual', 'loops': 'financial',
                     'consolidation': 'spiritual', 'rollups': 'spiritual', 'refs': 'spiritual',
                     'cycle1': 'health', 'cycle2': 'health'}
    for table, user, identifier, domain in updates:
        next(row for row in tables[table] if row['id'] == identifier and row['user_id'] == user)['domain'] = domain
    assert plan_relabels(tables) == []


def test_real_sqlite_rollup_keeps_restricted_input_domain(monkeypatch):
    from tests.unit.test_vnext_rollups import _live_store, _seed_live_game_memories, _rollup_candidates
    from alicebot_api.vnext_rollups import VNextRollupService
    monkeypatch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
    conn, store = _live_store()
    try:
        members = _seed_live_game_memories(store)
        for index, row in enumerate(members.values()):
            store.update_memory(memory_id=str(row['id']), patch={'domain': 'health' if index % 2 else 'project'})
        outcome = VNextRollupService(store).propose_rollups()
        candidates = _rollup_candidates(store)
        assert candidates and outcome.proposals
        assert all(row['domain'] == 'health' for row in candidates)
    finally:
        conn.close()


def test_consolidation_report_includes_rollup_input_domains(monkeypatch):
    from tests.unit.test_vnext_consolidation import FakeConsolidationStore
    from tests.unit.test_vnext_rollups import _seed_game_memories
    from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
    monkeypatch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
    store = FakeConsolidationStore()
    members = _seed_game_memories(store)
    for index, row in enumerate(store.memories):
        row['domain'] = 'health' if index % 2 else 'project'
    artifact = VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(MemoryConsolidationRequest())
    assert artifact['metadata_json']['rollups']['proposals']
    assert artifact['domain'] == 'health'


def test_belief_reference_follows_a_repaired_derived_memory():
    from alicebot_api.vnext_derived_domain_backfill import plan_relabels
    tables = {'sources': [{'id': 'source', 'user_id': 'u', 'domain': 'health'}],
              'memories': [{'id': 'derived-belief', 'user_id': 'u', 'domain': 'unknown',
                            'metadata_json': {'workflow': 'project_auto_update', 'source_ids': ['source']}}],
              'beliefs': [{'id': 'b', 'user_id': 'u', 'memory_id': 'derived-belief'}],
              'generated_artifacts': [{'id': 'report', 'user_id': 'u', 'domain': 'unknown',
                                      'metadata_json': {'belief_ids': ['b']}}]}
    assert {row[2]: row[3] for row in plan_relabels(tables)} == {'derived-belief': 'health', 'report': 'health'}


def test_unrestricted_rollup_only_report_retains_previous_labels(monkeypatch):
    from tests.unit.test_vnext_consolidation import FakeConsolidationStore
    from tests.unit.test_vnext_rollups import _seed_game_memories
    from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
    monkeypatch.delenv('ALICE_EMBEDDINGS_BASE_URL', raising=False)
    store = FakeConsolidationStore()
    _seed_game_memories(store)
    artifact = VNextConsolidationService(store, embedding_provider=None).generate_memory_consolidation(MemoryConsolidationRequest())
    assert artifact['metadata_json']['rollups']['proposals']
    assert (artifact['domain'], artifact['sensitivity']) == ('unknown', 'unknown')


@pytest.mark.parametrize('workflow', ('daily_brief', 'weekly_synthesis', 'connection_report', 'contradiction_report', 'project_update', 'staleness_sweep', 'open_loop_review'))
def test_every_report_producer_retains_restricted_inputs(workflow):
    if workflow in ('daily_brief', 'weekly_synthesis'):
        from tests.unit.test_vnext_brain import _seed_store
        from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
        store = _seed_store()
        store.sources[0]['domain'] = 'health'
        artifact = getattr(VNextBrainService(store), 'generate_' + workflow)(BrainArtifactRequest(generated_for='2026-05-10'))
        if workflow == 'weekly_synthesis':
            assert store.memories[-1]['domain'] == 'health'
            assert store.memories[-1]['metadata_json']['input_summary']['source_ids']
    elif workflow == 'connection_report':
        from tests.unit.test_vnext_connections import _seed_store
        from alicebot_api.vnext_connections import ConnectionFinderRequest, VNextConnectionService
        store = _seed_store()
        store.sources[0]['domain'] = 'health'
        artifact = VNextConnectionService(store).generate_connection_report(ConnectionFinderRequest())
    elif workflow == 'contradiction_report':
        from tests.unit.test_vnext_contradictions import _seed_store
        from alicebot_api.vnext_contradictions import ContradictionFinderRequest, VNextContradictionService
        store = _seed_store()
        store.sources[0]['domain'] = 'health'
        artifact = VNextContradictionService(store).generate_contradiction_report(ContradictionFinderRequest())
    elif workflow == 'project_update':
        from tests.unit.test_vnext_projects import _seed_store
        from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
        store = _seed_store()
        store.sources[0]['domain'] = 'health'
        artifact = VNextProjectService(store).generate_project_update_candidate(ProjectAutomationRequest(project_id='project-1'))
        assert store.memories[artifact['metadata_json']['candidate_memory_id']]['domain'] == 'health'
    else:
        from tests.unit.test_vnext_scheduler import _staleness_store
        from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
        store = _staleness_store()
        store.memories[0]['domain'] = 'health'
        store.open_loops = [{'id': 'loop-health', 'title': 'Review', 'status': 'open', 'domain': 'health', 'sensitivity': 'private'}]
        result = VNextSchedulerService(store).run_now(SchedulerRunRequest(workflow_type=workflow, generated_for='2026-07-04', options={'reference_time': '2026-07-04T03:30:00Z'}))
        artifact = result['artifact']
    assert artifact['domain'] == 'health'
