"""The label limit of a queue claim.

``PostgresVNextStore.claim_next_task`` takes the oldest due pending task. A caller with limits passes the
(domain, sensitivity) pairs it may read, and the claim is limited to tasks with one of those pairs. The limit sits in
the CTE that picks the task, before the row lock and the ``LIMIT 1``, so a task outside it is skipped and not locked
and a worker with no limits still takes it.

The CTE hands the picked id to the ``UPDATE`` as ``next_id``. Named ``id`` it would be a second ``id`` beside the
column of ``task_queue``, and PostgreSQL refuses ``RETURNING id`` as ambiguous.
"""

from __future__ import annotations

from collections.abc import Collection

LabelPairs = Collection[tuple[str, str]]


def task_label_filter(readable_labels: LabelPairs | None) -> tuple[str, tuple[object, ...] | None]:
    """The SQL condition for the claim and its parameters. ``None`` is no limit, and no pair claims nothing."""

    if readable_labels is None:
        return "", None
    pairs = sorted(set(readable_labels))
    condition = "AND (domain, sensitivity) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
    return condition, ([domain for domain, _sensitivity in pairs], [sensitivity for _domain, sensitivity in pairs])
