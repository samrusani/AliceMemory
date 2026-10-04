"""Run each guard's regression with that guard removed, restoring it afterwards.

Run from the repository root: PYTHONPATH=apps/api/src:workers:. python scripts/check_entity_fence_mutations.py
No production files are written. Each replacement is compiled from the actual
production function and installed only for its one regression invocation.
"""
from __future__ import annotations

import inspect
import textwrap
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from alicebot_api import vnext_retrieval as retrieval, vnext_grounding as grounding
from alicebot_api.mcp import evidence_artifacts as audit
from tests.unit import test_entity_disclosure_fence as checks


def kill(owner, name, before, after, check):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    assert before in source, f'mutation no longer matches: {name}: {before}'
    namespace = dict(original.__globals__)
    exec(compile(source.replace(before, after, 1), '<guard mutation>', 'exec'), namespace)
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


def temporary_check(check, *, vault=False):
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        if vault:
            fixture = checks.entity_vault.__wrapped__(SimpleNamespace(mktemp=lambda name: root))
            check(fixture, patch)
        else:
            check(root)


def main():
    graph = retrieval.VNextRetrievalService
    mutations = [
        (graph, '_memory_graph_rows', 'and not (memory_types or created_by_agent_ids or run_id or scope_thread_id or scope_task_id)', '', checks.test_source_only_entities_do_not_bypass_memory_specific_filters),
        (graph, '_memory_graph_rows', 'or edge.get("valid_to") is not None', '', checks.test_source_only_entities_do_not_bypass_memory_specific_filters),
        (audit, '_handle_alice_vnext_memory_audit', 'identity is None or (identity.permission_profile == "admin_agent" and not identity.project_scope)', 'True', lambda: temporary_check(checks.test_explain_real_key_count_fence, vault=True)),
        (graph, 'compile_context_pack', 'allow_entity_lookup=(not domains or set(VNEXT_DOMAINS).issubset(domains))\n                and set(ALL_SENSITIVITY).issubset(sensitivity_allowed)', 'allow_entity_lookup=True', lambda: temporary_check(checks.test_context_pack_passes_the_entity_fence_to_grounding)),
        (graph, '_memory_graph_rows', 'entities = [entity for entity in entities if str(entity.get("id")) in visible_entity_ids]', 'entities = list(entities)', checks.test_hidden_entity_matches_absent_entity_including_debug_status),
        (graph, '_memory_graph_rows', 'include_count=not restricted', 'include_count=True', checks.test_fenced_count_is_omitted_but_unrestricted_count_survives),
        (graph, '_memory_graph_rows', 'if not restricted:\n        entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]', 'if True:\n        entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]', checks.test_fenced_selection_ignores_hidden_counts_and_filters_before_limit),
        (graph, '_memory_graph_rows', 'entities.sort(key=lambda entity: (str(entity.get("name")), str(entity.get("entity_type")), str(entity.get("id"))))', 'entities.sort(key=lambda entity: -int(entity.get("mention_count", 0)))', checks.test_filtered_entity_order_does_not_follow_stored_counts),
        (graph, '_memory_graph_rows', 'GRAPH_STAGE_ENABLED if entities else GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH', 'GRAPH_STAGE_ENABLED', checks.test_hidden_entity_matches_absent_entity_including_debug_status),
        (graph, '_memory_graph_rows', '_allowed(source, domains=domains, sensitivity_allowed=sensitivity_allowed) is None', 'True', lambda: checks.test_source_only_entities_obey_domain_sensitivity_and_project(False)),
        (graph, '_memory_graph_rows', '_row_matches_scope(source, source_scope, source_scope_envelope=True)', 'True', lambda: checks.test_source_only_entities_obey_domain_sensitivity_and_project(True)),
        (graph, '_memory_graph_rows', 'source.get("deleted_at") is None', 'True', lambda: checks.test_source_only_entities_obey_domain_sensitivity_and_project(False)),
        (grounding, 'corpus_support', 'allow_entity_lookup and store_supports_entity_linking(store)', 'store_supports_entity_linking(store)', checks.test_grounding_does_not_treat_a_hidden_entity_as_corpus_support),
        (audit, '_memory_linked_entities', 'if include_entity_counts else {}', 'if True else {}', checks.test_explain_omits_counts_when_fenced),
        (retrieval, '_row_matches_scope', 'scope.window_start is not None or scope.window_end is not None', 'scope.window_start is not None', checks.test_source_entity_obeys_an_until_only_filter),
    ]
    for mutation in mutations:
        kill(*mutation)
    print(f'{len(mutations)}/{len(mutations)} guard mutations killed')


if __name__ == '__main__':
    main()
