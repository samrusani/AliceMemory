"""Restricted-domain generation and migration on the role-separated database."""
from uuid import uuid4

from alembic import command

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_store import PostgresVNextStore


def test_postgres_derived_domain_upgrade_and_generation(database_urls):
    config = make_alembic_config(database_urls['admin'])
    command.upgrade(config, '20260721_0094')
    user = uuid4()
    with user_connection(database_urls['app'], user) as conn:
        ContinuityStore(conn).create_user(user, 'derived-fence@example.invalid', 'Derived fence')
        store = PostgresVNextStore(conn)
        source = store.create_memory({'memory_key': 'health', 'canonical_text': 'Cedar observation',
            'domain': 'health', 'sensitivity': 'public', 'status': 'active'})
        store.create_memory({'memory_key': 'project', 'canonical_text': 'Project observation',
            'domain': 'project', 'sensitivity': 'public', 'status': 'active'})
        derived = store.create_memory({'memory_key': 'rollup', 'canonical_text': 'Derived text', 'domain': 'unknown',
            'status': 'candidate', 'metadata_json': {'consolidation': {'cluster_member_ids': [str(source['id'])]}}})
        report = store.create_artifact({'artifact_type': 'daily_brief', 'title': 'Earlier brief', 'content_markdown': 'Derived text',
            'status': 'needs_review', 'domain': 'unknown', 'sensitivity': 'public',
            'metadata_json': {'input_summary': {'memory_ids': [str(derived['id'])]}}})
    command.upgrade(config, 'head')
    with user_connection(database_urls['app'], user) as conn:
        store = PostgresVNextStore(conn)
        repaired = store.get_memory(str(derived['id']))
        assert repaired['domain'] == 'health'
        assert {k: v for k, v in repaired.items() if k != 'domain'} == {k: v for k, v in derived.items() if k != 'domain'}
        assert store.get_artifact(str(report['id']))['domain'] == 'health'
        fresh = VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(sensitivity_allowed=ALL_SENSITIVITY, discover_open_loops=False))
        assert fresh['domain'] == 'health'
    # The data-only downgrade retains safe labels, and repeating the upgrade is harmless.
    command.downgrade(config, '20260721_0094')
    command.upgrade(config, 'head')
    with user_connection(database_urls['app'], user) as conn:
        assert PostgresVNextStore(conn).get_memory(str(derived['id'])) == repaired
