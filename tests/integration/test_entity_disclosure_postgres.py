"""The entity read fence against real PostgreSQL rows and graph SQL."""
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, evaluate_agent_policy
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_retrieval import VNextRetrievalService
from alicebot_api.vnext_store import PostgresVNextStore


def test_entity_names_and_counts_follow_the_postgres_read_fence(migrated_database_urls):
    user = uuid4()
    with user_connection(migrated_database_urls['app'], user) as conn:
        ContinuityStore(conn).create_user(user, 'entity-fence@example.invalid', 'Entity fence')
        store = PostgresVNextStore(conn)
        _, raw = create_agent_key(store, user_id=user, agent_id='reader', permission_profile='read_only_agent', project_scope='alpha')
        identity = resolve_agent_identity(store, user_id=user, raw_key=raw, payload={})
        decision = evaluate_agent_policy(identity=identity, action='memory.recall', sensitivity_allowed=ALL_SENSITIVITY)
        for name, domain, project in [('Meridian', 'project', 'alpha'), ('Cedar', 'health', 'alpha'), ('Briar', 'project', 'beta')]:
            entity = store.create_entity({'name': name, 'entity_type': 'person', 'mention_count': 900})
            memory = store.create_memory({'memory_key': name, 'canonical_text': name + ' observation', 'status': 'active',
                'domain': domain, 'sensitivity': 'public', 'project_id': project, 'metadata_json': {'project_scope': [project]}})
            store.create_edge({'from_type': 'memory', 'from_id': memory['id'], 'to_type': 'entity', 'to_id': entity['id'], 'edge_type': 'mentions'})
        service = VNextRetrievalService(store)
        _, _, entities = service._memory_graph_rows(query='Meridian Cedar Briar', domains=list(decision.effective_domains),
            sensitivity_allowed=list(decision.effective_sensitivity_allowed), projects=decision.effective_project_scope, limit=10)
        assert [row['name'] for row in entities] == ['Meridian']
        assert 'mention_count' not in entities[0]
        _, _, owner = service._memory_graph_rows(query='Meridian Cedar Briar', domains=[], sensitivity_allowed=list(ALL_SENSITIVITY), limit=10)
        assert {row['name'] for row in owner} == {'Meridian', 'Cedar', 'Briar'}
        assert all(row['mention_count'] == 900 for row in owner)
