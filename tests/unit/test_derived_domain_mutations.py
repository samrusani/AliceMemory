"""Execute regressions with each derived-domain fence removed in memory.

Run by the ordinary unit suite in CI.
Production files are not written. A surviving mutation is a failing run.
"""
from __future__ import annotations

import inspect
import sys
import tempfile
import textwrap
from pathlib import Path
from types import FunctionType

import pytest

from alicebot_api import vnext_brain as brain, vnext_consolidation as consolidation
from alicebot_api import vnext_connections as connections, vnext_contradictions as contradictions
from alicebot_api import vnext_projects as projects, vnext_scheduler as scheduler
from alicebot_api import vnext_derived_domain as domain, vnext_derived_domain_backfill as backfill
from tests.unit import test_derived_domain_fence as checks
from tests.unit import test_derived_domain_review as review
from tests.unit import test_derived_domain_stored_ids as stored_ids
from alicebot_api import onramp, sqlite_schema
from alicebot_api.vnext_label_writes import without_insert_floor


def kill(owner, name, before, after, check):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    if before not in source and before.replace("'", '"') in source:
        before, after = before.replace("'", '"'), after.replace("'", '"')
    assert before in source, f'mutation no longer matches: {name}: {before}'
    namespace = dict(original.__globals__)
    exec(compile(source.replace(before, after, 1), '<guard mutation>', 'exec'), namespace)
    compiled = namespace[name]
    replacement = FunctionType(compiled.__code__, original.__globals__, name, compiled.__defaults__, compiled.__closure__)
    replacement.__kwdefaults__ = compiled.__kwdefaults__
    namespace[name] = replacement
    aliases = [(module, key) for module in list(sys.modules.values())
               if module and (getattr(module, "__name__", "").startswith("alicebot_api")
                              or module in (checks, review))
               for key, value in list(vars(module).items()) if value is original]
    for module, key in aliases:
        setattr(module, key, namespace[name])
    setattr(owner, name, namespace[name])
    try:
        try:
            check()
        except (AssertionError, pytest.fail.Exception):
            print(f'KILLED {name}: {before}')
        else:
            raise RuntimeError(f'SURVIVED {name}: {before}')
    finally:
        setattr(owner, name, original)
        for module, key in aliases:
            setattr(module, key, original)


def selector_check():
    assert domain.derived_domain([{'domain': name} for name in ('project', 'project', 'project', 'legal', 'health')], fallback='unknown') == 'health'
    assert domain.derived_domain([{'domain': name} for name in ('health', 'legal', 'legal')], fallback='unknown') == 'legal'
    assert domain.derived_domain([{'domain': 'project'}], fallback='professional') == 'professional'


def chain():
    fresh(lambda directory, patch: review.test_a_chain_without_a_cycle_settles_reading_each_row_once(patch, 16))


def budget():
    fresh(lambda directory, patch:
          review.test_a_cycle_is_refused_after_six_changes_for_each_of_its_rows_whatever_the_graph_around_it(patch))


def fresh(check):
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        check(Path(directory), patch)


def restore(directory, patch):
    review.test_restore_repairs_derived_rows_before_publication(directory, patch, True)


def test_derived_domain_guard_mutations():
    mutations = [
        (domain, 'derived_domain', "if row.get('domain') in RESTRICTED_DOMAINS", 'if True', selector_check),
        (domain, 'derived_domain', '-counts[domain]', 'counts[domain]', selector_check),
        (domain, 'derived_domain', 'min(counts, key=lambda domain: (-counts[domain], domain))', 'counts.most_common(1)[0][0]', selector_check),
        (domain, 'derived_domain', 'if counts else fallback', "if counts else 'unknown'", selector_check),
        (backfill, '_settle', "if metadata.get('redacted') is True:", 'if False:', checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (brain, '_artifact_domain', 'derived_domain(rows, fallback=request.domains[0])', 'request.domains[0]', checks.test_restricted_majority_ties_and_explicit_request),
        (brain, '_artifact_domain', 'derived_domain(rows, fallback="unknown")', '"unknown"', lambda: checks.test_every_report_producer_retains_restricted_inputs('daily_brief')),
        (consolidation, '_domain', 'derived_domain(rows, fallback=request.domains[0])', 'request.domains[0]', checks.test_restricted_majority_ties_and_explicit_request),
        (backfill, '_settle', "(kind, user, row_id) for kind, row_id in refs", "(kind, 'missing-user', row_id) for kind, row_id in refs", checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (backfill, '_settle', "and metadata.get('input_summary')", "and False", checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
        (backfill, '_settle', "if dependant not in queued:", "if False:", review.test_a_cycle_that_settles_reads_its_rows_again),
        (backfill, '_settle', "for component in _input_groups(inputs):", "for component in reversed(_input_groups(inputs)):", chain),
        (backfill, '_settle', "dependants.get(key, set()) & members", "dependants.get(key, set())", chain),
        (backfill, '_input_groups', "lowest[key] = min(lowest[key], index[ref])", "pass", unsettled_message),
        (backfill, '_input_groups', "lowest[parent] = min(lowest[parent], lowest[key])", "pass", unsettled_message),
        (backfill, '_input_groups', "lowest[key] = min(lowest[key], index[ref])", "lowest[key] = index[ref]",
         review.test_one_cycle_with_two_back_edges_is_one_group_and_settles),
        (backfill, '_input_groups', "if ref not in index:", "if ref not in on_stack:",
         lambda: fresh(lambda directory, patch: review.test_rows_that_share_inputs_are_each_read_once(patch))),
        (backfill, '_settle', "remaining_changes = len(component) *", "remaining_changes = len(inputs) *", budget),
        (backfill, '_settle', "len(component) * (len(RESTRICTED_DOMAINS) + 1)", "len(component) * 2", budget),
        (backfill, '_settle', "len(component) * (len(RESTRICTED_DOMAINS) + 1)", "len(component) * len(RESTRICTED_DOMAINS)", budget),
        (backfill, '_settle', "if remaining_changes < 0:", "if remaining_changes <= 0:", budget),
        (backfill, '_settle', "ref[0] == 'beliefs' and ref in rows", 'False', checks.test_belief_reference_follows_a_repaired_derived_memory),
        (backfill, 'recorded_inputs', "elif key == 'source_refs':", 'elif False:', checks.test_recorded_input_shapes_chains_cycles_users_and_redaction),
    ]
    mutations.extend([
        (backfill, "_settle", "or _object(row.get('value')).get('kind') == 'promoted_artifact'", "or False",
         lambda: review.test_promoted_artifact_memory_follows_repaired_artifact("value")),
        (backfill, "_settle", "or isinstance(metadata.get('source_artifact_id'), str)", "or False",
         promoted_metadata_only),
        (backfill, "recorded_inputs", "if key in _ID_KEYS:", "if key in _ID_KEYS and key != 'artifact_id':",
         lambda: review.test_promoted_artifact_memory_follows_repaired_artifact("value")),
        (backfill, "recorded_inputs", "if key in _ID_KEYS:", "if key in _ID_KEYS and key != 'source_artifact_id':",
         lambda: review.test_promoted_artifact_memory_follows_repaired_artifact("metadata")),
        (backfill, "_settle", "if remaining_changes < 0:", "if False:",
         lambda: fresh(lambda directory, patch: review.test_nonsettling_cycles_are_bounded_and_not_published(patch))),
        (backfill, "_settle", "sorted(row for row, count in changes.items() if count > 1)", "[]",
         unsettled_message),
        (backfill, "_settle", "raise DerivedDomainRepairError(", "raise ValueError(",
         unsettled_message),
        (backfill, "_unsettled_message", "Remove the circular input references from those rows, ", "",
         unsettled_message),
        (onramp, "_run_import_snapshot", 'raise _ImportError("the restored derived labels could not be settled") from exc', "raise",
         lambda: fresh(lambda directory, patch: review.test_import_of_a_backup_holding_a_cycle_stops_before_publication(directory, patch))),
        (backfill, "relabel_sqlite", "if not restoring and conn.execute", "if conn.execute",
         lambda: fresh(restore)),
        (onramp, "_run_import_snapshot", "relabel_sqlite(conn, restoring=True)", "None",
         lambda: fresh(restore)),
        (onramp, "_import_records", "candidates.append(tuple(repaired))", "None",
         lambda: fresh(restore)),
        (backfill, "recorded_sqlite_domain_repairs", "repairs.add(", "set().add(",
         lambda: fresh(restore)),
        (backfill, "relabel_event", 'event_type=f"{target_type}.domain_relabelled"', 'event_type="repair.omitted"',
         lambda: fresh(lambda directory, patch: review.test_repair_records_changed_rows_once(directory))),
    ])
    mutations.append((backfill, "_identifier", "return str(UUID(str(value)))", "return str(value)",
                      lambda: review.test_promoted_artifact_uuid_aliases_resolve("value")))
    # The SQLite repair updates and records a row under the id it is stored with.
    mutations.extend([
        (backfill, "relabel_sqlite", "(domain, user, stored)", "(domain, user, _identifier(stored))", stored_open),
        (backfill, "relabel_sqlite", 'event["target_type"],\n                stored,',
         'event["target_type"],\n                _identifier(stored),', stored_open),
        (backfill, "relabel_sqlite", 'event["target_type"],\n                stored,',
         'event["target_type"],\n                _identifier(stored),', stored_second_restore),
        # Two stored spellings of one id are each compared with the label their own inputs give.
        (backfill, "plan_stored_relabels", "for row in values:",
         "for row in {_identifier(item['id']): item for item in values}.values():", stored_twins),
        (backfill, "plan_stored_relabels", "for row in values:",
         "for row in {_identifier(item['id']): item for item in reversed(values)}.values():", stored_twins),
        (backfill, "plan_stored_relabels", "for row in values:",
         "for row in {_identifier(item['id']): item for item in values}.values():", stored_last_row_planned),
        (backfill, "plan_stored_relabels", "if domain in RESTRICTED_DOMAINS and domain != previous:",
         "if domain in RESTRICTED_DOMAINS:", stored_settled_twin),
        (backfill, "plan_stored_relabels", "fallback=str(previous)", "fallback=str(labels[key])", stored_no_change),
        (backfill, "plan_stored_relabels", "{'domain': labels[ref]}", "{'domain': 'unknown'}", stored_parity),
        (backfill, "require_changed", "if changed == 0:", "if changed < 0:", stored_zero_row),
        (backfill, "require_changed", "raise DerivedDomainRepairError(", "raise ValueError(", stored_zero_row),
        (backfill, "relabel_sqlite", "(domain, user, stored)", "(domain, user, _identifier(stored))", stored_restore),
    ])
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
        with pytest.MonkeyPatch.context() as patch, without_insert_floor():
            patch.setattr(module, 'derived_domain', lambda rows, *, fallback: fallback)
            with pytest.raises(AssertionError):
                check(patch)
        print('KILLED producer selector:', module.__name__)
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch, without_insert_floor():
        patch.setattr(sqlite_schema, '_relabel_derived_domains', lambda conn: None)
        with pytest.raises(AssertionError):
            checks.test_sqlite_upgrade_relabels_existing_derived_memory_only(Path(directory))
    print('KILLED SQLite upgrade wiring')
    print(f'{len(mutations) + len(pairs) + 3} guard mutations killed')


def refusal_fails(check):
    """A refusal that leaks out of the check (an update that changed no row, or the wrong class of error) counts as the
    check failing."""

    try:
        check()
    except ValueError as error:
        raise AssertionError(str(error)) from error


def stored_open():
    refusal_fails(lambda: fresh(
        lambda directory, patch: stored_ids.test_open_relabels_a_row_stored_under_another_spelling(directory, patch, "upper")))


def stored_restore():
    refusal_fails(lambda: fresh(
        lambda directory, patch: stored_ids.test_restore_relabels_a_row_stored_under_another_spelling(directory, patch, "compact")))


def stored_second_restore():
    fresh(stored_ids.test_the_same_backup_restores_again_into_the_repaired_vault)


def stored_twins():
    refusal_fails(lambda: fresh(stored_ids.test_every_spelling_of_one_id_is_relabelled))


def stored_last_row_planned():
    refusal_fails(lambda: fresh(stored_ids.test_the_earlier_spelling_is_relabelled_when_the_last_row_already_holds_the_planned_label))


def stored_no_change():
    for first, last in (("health", "unknown"), ("health", "legal"), ("legal", "health")):
        refusal_fails(lambda: fresh(
            lambda directory, patch: stored_ids.test_no_spelling_changes_label_when_the_inputs_name_no_restricted_label(
                directory, patch, first, last)))


def stored_parity():
    stored_ids.test_for_rows_with_unique_ids_the_stored_plan_is_the_plan()


def stored_settled_twin():
    refusal_fails(lambda: fresh(stored_ids.test_a_spelling_already_at_the_planned_label_is_left_alone))


def stored_zero_row():
    refusal_fails(lambda: fresh(stored_ids.test_a_zero_row_update_refuses_without_an_event_or_stamp))
    refusal_fails(lambda: fresh(stored_ids.test_a_refusal_writes_no_event_and_no_stamp_even_after_an_earlier_update))


def unsettled_message():
    fresh(lambda directory, patch: review.test_a_nonsettling_cycle_fails_with_a_message_that_names_the_rows_and_the_way_out(patch))


def promoted_metadata_only():
    tables = {"generated_artifacts": [{"id": "report", "user_id": "u", "domain": "health"}],
              "memories": [{"id": "copy", "user_id": "u", "domain": "unknown",
                            "metadata_json": {"source_artifact_id": "report"}}]}
    assert backfill.plan_relabels(tables) == [("memories", "u", "copy", "health")]


def test_staleness_sensitivity_mutation():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduler.VNextSchedulerService, "_highest_sensitivity", lambda self, rows: "unknown")
        with pytest.raises(AssertionError):
            review.test_staleness_report_keeps_title_sensitivity(patch)
