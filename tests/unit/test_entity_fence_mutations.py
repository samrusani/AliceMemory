"""CI-executed mutations of the entity admission and caller-wiring boundaries.

Expired-edge and deleted-source helper mutations from the earlier script were
withdrawn as real-store proofs: both stores already exclude those rows. Their
actual lifecycle behavior is covered by the PostgreSQL integration cases.
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
from tests.unit import test_entity_fence_review as review_checks
from tests.unit import test_entity_owner_parity as parity_checks
from alicebot_api.mcp import retrieval as recall
from alicebot_api.mcp import registry


def kill(owner, name, before, after, check):
    original = getattr(owner, name)
    function = inspect.unwrap(original.fget if isinstance(original, property) else original)
    source = textwrap.dedent(inspect.getsource(function))
    assert before in source, f'mutation no longer matches: {name}: {before}'
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after, 1), '<guard mutation>', 'exec'), namespace)
    aliases = [(module, key) for module in (retrieval, grounding, audit, recall, registry)
               for key, value in vars(module).items() if value is original]
    for module, key in aliases:
        setattr(module, key, namespace[name])
    setattr(owner, name, namespace[name])
    registered = [tool for tool, handler in registry._TOOL_HANDLERS.items() if handler is original]
    for tool in registered:
        registry._TOOL_HANDLERS[tool] = namespace[name]
    try:
        try:
            check()
        except AssertionError:
            print(f'KILLED {name}: {before}')
        else:
            raise RuntimeError(f'SURVIVED {name}: {before}')
    finally:
        setattr(owner, name, original)
        for module, key in aliases:
            setattr(module, key, original)
        for tool in registered:
            registry._TOOL_HANDLERS[tool] = original


def temporary_check(check, *, vault=False):
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        if vault:
            fixture = checks.entity_vault.__wrapped__(SimpleNamespace(mktemp=lambda name: root))
            check(fixture, patch)
        else:
            check(root)


def test_entity_guard_mutations():
    graph = retrieval.VNextRetrievalService
    mutations = [
        (graph, '_memory_graph_rows', 'and not (memory_types or created_by_agent_ids or run_id or scope_thread_id or scope_task_id)', '', checks.test_source_only_entities_do_not_bypass_memory_specific_filters),
        (audit, '_handle_alice_vnext_memory_audit', 'not SourceReadFence.for_identity(identity).entity_read_fenced', 'True', lambda: temporary_check(checks.test_explain_real_key_count_fence, vault=True)),
        (graph, 'compile_context_pack', 'allow_entity_lookup=not source_fence.entity_read_fenced', 'allow_entity_lookup=True', lambda: temporary_check(checks.test_context_pack_passes_the_entity_fence_to_grounding)),
        (graph, '_memory_graph_rows', 'entities = [entity for entity in entities if str(entity.get("id")) in visible_entity_ids]', 'entities = list(entities)', checks.test_hidden_entity_matches_absent_entity_including_debug_status),
        (graph, '_memory_graph_rows', 'include_count=not restricted', 'include_count=True', checks.test_fenced_count_is_omitted_but_unrestricted_count_survives),
        (graph, '_memory_graph_rows', 'if not restricted:\n        entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]', 'if True:\n        entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]', checks.test_fenced_selection_ignores_hidden_counts_and_filters_before_limit),
        (graph, '_memory_graph_rows', '-len(readable_mentions[str(entity.get("id"))])', '-int(entity.get("mention_count", 0))', checks.test_filtered_entity_order_does_not_follow_stored_counts),
        (graph, '_memory_graph_rows', 'GRAPH_STAGE_ENABLED if entities else GRAPH_STAGE_DISABLED_NO_ENTITY_MATCH', 'GRAPH_STAGE_ENABLED', checks.test_hidden_entity_matches_absent_entity_including_debug_status),
        (graph, '_memory_graph_rows', '_allowed(source, domains=domains, sensitivity_allowed=sensitivity_allowed) is None', 'True', lambda: checks.test_source_only_entities_obey_domain_sensitivity_and_project(False)),
        (graph, '_memory_graph_rows', '_row_matches_scope(source, source_scope, source_scope_envelope=True)', 'True', lambda: checks.test_source_only_entities_obey_domain_sensitivity_and_project(True)),
        (audit, '_memory_linked_entities', 'if include_entity_counts else {}', 'if True else {}', checks.test_explain_omits_counts_when_fenced),
        (retrieval, '_row_matches_scope', 'scope.window_start is not None or scope.window_end is not None', 'scope.window_start is not None', checks.test_source_entity_obeys_an_until_only_filter),
    ]
    mutations.extend([
        (recall, '_handle_alice_recall', 'entity_read_fenced=source_fence.entity_read_fenced', 'entity_read_fenced=False', lambda: real_count_check('read_only_agent', 'alice_recall')),
        (graph, 'compile_context_pack', 'entity_read_fenced=source_fence.entity_read_fenced', 'entity_read_fenced=False', lambda: real_count_check('read_only_agent', 'alice_context_pack')),
        (recall, '_handle_alice_recall', 'entity_read_fenced=source_fence.entity_read_fenced', 'entity_read_fenced=True', lambda: real_count_check(None, 'alice_recall')),
        (graph, 'compile_context_pack', 'entity_read_fenced=source_fence.entity_read_fenced', 'entity_read_fenced=True', lambda: real_count_check('admin_agent', 'alice_context_pack')),
        (graph, '_memory_graph_rows', 'people=frozenset(scope_people)', 'people=frozenset()', lambda: review_checks.test_source_only_entity_obeys_person_and_since_filters('person')),
        (graph, '_memory_graph_rows', 'window_start=scope_window_start, window_end=scope_window_end,', 'window_start=None, window_end=scope_window_end,', lambda: review_checks.test_source_only_entity_obeys_person_and_since_filters('since')),
        (graph, '_memory_graph_rows', 'readable_mentions[entity_id].add(("memory", memory_id))', 'None', review_checks.test_fenced_entity_seeds_rank_by_readable_mentions),
        (graph, '_memory_graph_rows', 'readable_mentions[entity_id].add(("source", source_id))', 'None', review_checks.test_readable_source_mentions_count_even_for_memory_linked_entities),
        (graph, '_memory_graph_rows', 'for entity_id in entity_ids:', 'for entity_id in entity_ids - visible_entity_ids:', review_checks.test_readable_source_mentions_count_even_for_memory_linked_entities),
        (graph, 'compile_context_pack', 'admitted_entity_names=tuple(str(entity["name"]) for entity in matched_entities),\n                admitted_entity_ids=tuple(str(entity["id"]) for entity in matched_entities)', 'admitted_entity_names=(), admitted_entity_ids=()', lambda: temporary_check(lambda root: review_checks.test_grounding_accepts_an_entity_admitted_through_a_readable_link(root, False))),
        (grounding, 'corpus_support', 'admitted = {normalize_entity_name(name) for name in admitted_entity_names}', 'admitted = set()', review_checks.test_admitted_display_name_without_table_lookup),
        (grounding, 'compute_query_grounding', 'admitted_entity_names=admitted_entity_names,\n        admitted_entity_ids=admitted_entity_ids', 'admitted_entity_names=(), admitted_entity_ids=()', lambda: temporary_check(lambda root: review_checks.test_grounding_accepts_an_entity_admitted_through_a_readable_link(root, False))),
    ])
    mutations.extend([
        (grounding, "corpus_support", "(allow_entity_lookup or admitted_entity_ids)", "allow_entity_lookup",
         lambda: temporary_check(lambda root: review_checks.test_grounding_accepts_an_entity_admitted_through_a_readable_link(root, True))),
        (grounding, "corpus_support", 'if not allow_entity_lookup and str(row.get("id")) not in admitted_entity_ids:', "if False:",
         lambda: temporary_check(review_checks.test_alias_grounding_never_admits_an_unreadable_entity)),
    ])
    from alicebot_api.vnext_source_fence import SourceReadFence
    mutations.append((SourceReadFence, "entity_read_fenced", "or bool(decision.effective_project_scope)", "or False",
                      review_checks.test_declared_project_scope_is_part_of_entity_policy))
    def unlinked_names():
        temporary_check(parity_checks.test_owner_and_unbound_admin_keep_main_grounding_for_unlinked_entities)

    mutations.extend([
        (graph, 'compile_context_pack', 'allow_entity_lookup=not source_fence.entity_read_fenced', 'allow_entity_lookup=False', unlinked_names),
        (SourceReadFence, 'entity_read_fenced', 'decision.decision != "allowed"', 'True', unlinked_names),
        (graph, '_memory_graph_rows', 'entity_read_fenced: bool,', 'entity_read_fenced: bool = False,', review_checks.test_the_entity_fence_argument_is_required_and_keyword_only),
        (graph, '_memory_graph_rows', '*,\n    query: str,', 'query: str,', review_checks.test_the_entity_fence_argument_is_required_and_keyword_only),
        (graph, '_memory_graph_rows', 'entities = entities[:GRAPH_ENTITY_MATCH_LIMIT]\n        selected_entity_ids', 'entities = entities[:GRAPH_ENTITY_MATCH_LIMIT + 1]\n        selected_entity_ids', review_checks.test_fenced_cap_keeps_five_names_and_drops_graph_rows_linked_only_to_the_rest),
        (graph, '_memory_graph_rows', 'ranked = [entry for entry in ranked if entities_by_memory[entry[2]] & selected_entity_ids]', 'pass', review_checks.test_fenced_cap_keeps_five_names_and_drops_graph_rows_linked_only_to_the_rest),
    ])
    for mutation in mutations:
        kill(*mutation)
    print(f'{len(mutations)}/{len(mutations)} guard mutations killed')


def real_count_check(profile, tool):
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        review_checks.test_default_entity_counts_follow_identity(Path(directory), patch, profile, tool)
