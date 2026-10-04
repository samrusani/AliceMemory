"""Execute regressions with each derived-domain fence removed in memory.

Run: PYTHONPATH=apps/api/src:workers:. python scripts/check_derived_domain_mutations.py
Production files are not written. A surviving mutation is a failing run.
"""
from __future__ import annotations

import inspect
import contextlib
import io
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from alicebot_api import vnext_brain as brain, vnext_consolidation as consolidation
from alicebot_api import vnext_connections as connections, vnext_contradictions as contradictions
from alicebot_api import vnext_projects as projects, vnext_scheduler as scheduler
from alicebot_api import vnext_derived_domain as domain, vnext_derived_domain_backfill as backfill
from tests.unit import test_derived_domain_fence as checks


def kill(owner, name, before, after, check):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    assert before in source, f'mutation no longer matches: {name}: {before}'
    namespace = dict(original.__globals__)
    exec(compile(source.replace(before, after, 1), '<guard mutation>', 'exec'), namespace)
    aliases = [key for key, value in vars(checks).items() if value is original]
    for key in aliases:
        setattr(checks, key, namespace[name])
    setattr(owner, name, namespace[name])
    try:
        try:
            check()
        except AssertionError:
            print(f'KILLED {name}: {before}')
        else:
            raise RuntimeError(f'SURVIVED {name}: {before}')
    finally:
        setattr(owner, name, original)
        for key in aliases:
            setattr(checks, key, original)


def selector_check():
    assert domain.derived_domain([{'domain': name} for name in ('project', 'project', 'project', 'legal', 'health')], fallback='unknown') == 'health'
    assert domain.derived_domain([{'domain': name} for name in ('health', 'legal', 'legal')], fallback='unknown') == 'legal'
    assert domain.derived_domain([{'domain': 'project'}], fallback='professional') == 'professional'


def main():
    mutations = [
        (domain, 'derived_domain', "if row.get('domain') in RESTRICTED_DOMAINS", 'if True', selector_check),
        (domain, 'derived_domain', '-counts[domain]', 'counts[domain]', selector_check),
        (domain, 'derived_domain', 'min(counts, key=lambda domain: (-counts[domain], domain))', 'counts.most_common(1)[0][0]', selector_check),
        (domain, 'derived_domain', 'if counts else fallback', "if counts else 'unknown'", selector_check),
        (backfill, 'plan_relabels', "if metadata.get('redacted') is True:", 'if False:', checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (brain, '_artifact_domain', 'derived_domain(rows, fallback=request.domains[0])', 'request.domains[0]', checks.test_restricted_majority_ties_and_explicit_request),
        (brain, '_artifact_domain', 'derived_domain(rows, fallback="unknown")', '"unknown"', lambda: checks.test_every_report_producer_retains_restricted_inputs('daily_brief')),
        (consolidation, '_domain', 'derived_domain(rows, fallback=request.domains[0])', 'request.domains[0]', checks.test_restricted_majority_ties_and_explicit_request),
        (backfill, 'plan_relabels', "(kind, user, row_id) for kind, row_id in refs", "(kind, 'missing-user', row_id) for kind, row_id in refs", checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (backfill, 'plan_relabels', "and metadata.get('input_summary')", "and False", checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (backfill, 'plan_relabels', "if not changed:", "if True:", checks.test_belief_reference_follows_a_repaired_derived_memory),
        (backfill, 'plan_relabels', "ref[0] == 'beliefs' and ref in rows", 'False', checks.test_belief_reference_follows_a_repaired_derived_memory),
        (backfill, 'recorded_inputs', "elif key == 'source_refs':", 'elif False:', checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
    ]
    for mutation in mutations:
        kill(*mutation)
    # Removing the shared selector independently at each producer must fail
    # that producer's persisted-output assertion.
    pairs = [(brain, 'weekly_synthesis'), (connections, 'connection_report'), (contradictions, 'contradiction_report'),
             (projects, 'project_update'), (scheduler, 'staleness_sweep'), (scheduler, 'open_loop_review')]
    for module, workflow in pairs:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, 'derived_domain', lambda rows, *, fallback: fallback)
            with pytest.raises(AssertionError):
                checks.test_every_report_producer_retains_restricted_inputs(workflow)
        print('KILLED producer selector:', workflow)
    from alicebot_api import vnext_rollups as rollups
    for module, check in [(rollups, checks.test_real_sqlite_rollup_keeps_restricted_input_domain),
                          (consolidation, checks.test_consolidation_report_includes_rollup_input_domains)]:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, 'derived_domain', lambda rows, *, fallback: fallback)
            with pytest.raises(AssertionError):
                check(patch)
        print('KILLED producer selector:', module.__name__)
    from alicebot_api import sqlite_schema
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite_schema, '_relabel_derived_domains', lambda conn: None)
        with pytest.raises(AssertionError):
            checks.test_sqlite_upgrade_relabels_existing_derived_memory_only(Path(directory))
    print('KILLED SQLite upgrade wiring')
    print(f'{len(mutations) + len(pairs) + 3} guard mutations killed')
    if '--postgres' in sys.argv:
        assert os.getenv('DATABASE_ADMIN_URL') and os.getenv('DATABASE_URL'), 'Set explicit disposable PostgreSQL URLs'
        output = io.StringIO()
        with pytest.MonkeyPatch.context() as patch, contextlib.redirect_stdout(output):
            patch.setattr(backfill, 'plan_relabels', lambda tables: [])
            code = pytest.main(['tests/integration/test_derived_domain_postgres.py', '-q', '--tb=short'])
        assert code == 1 and "assert 'unknown' == 'health'" in output.getvalue(), output.getvalue()
        print('KILLED PostgreSQL migration planner wiring')


if __name__ == '__main__':
    main()
