"""The label limit of a queue claim, and the payload of the event that records a task's new status.

``PostgresVNextStore.claim_next_task`` takes the oldest due pending task. A caller with limits passes the
(domain, sensitivity) pairs it may read, and the claim is limited to tasks with one of those pairs. The limit sits in
the CTE that picks the task, before the row lock and the ``LIMIT 1``, so a task outside it is skipped and not locked
and a worker with no limits still takes it.

The CTE hands the picked id to the ``UPDATE`` as ``next_id``. Named ``id`` it would be a second ``id`` beside the
column of ``task_queue``, and PostgreSQL refuses ``RETURNING id`` as ambiguous.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

LabelPairs = Collection[tuple[str, str]]


def task_label_filter(readable_labels: LabelPairs | None) -> tuple[str, tuple[object, ...] | None]:
    """The SQL condition for the claim and its parameters. ``None`` is no limit, and no pair claims nothing."""

    if readable_labels is None:
        return "", None
    pairs = sorted(set(readable_labels))
    condition = "AND (domain, sensitivity) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
    return condition, ([domain for domain, _sensitivity in pairs], [sensitivity for _domain, sensitivity in pairs])


def task_status_payload(status: str, details: Mapping[str, object]) -> dict[str, object]:
    """The payload of ``task.updated``: the new status, the details as given, and the artifacts the details name.

    A task that completes names the artifact it made inside its details, in the output column and in its metadata. The
    event guard reads the ids of labelled rows at the top of a payload and not inside ``details``, so the ids are also
    written at the top, as ``artifact_id`` (``artifact_ids`` when the two differ). A caller with limits is then shown the
    event only when it may read every artifact the event names.
    """

    payload: dict[str, object] = {"operation": "update_status", "status": status, "details": details}
    ids = _artifact_ids(details)
    if len(ids) == 1:
        payload["artifact_id"] = ids[0]
    elif ids:
        payload["artifact_ids"] = ids
    return payload


def _artifact_ids(details: Mapping[str, object]) -> list[str]:
    metadata = details.get("metadata_json")
    named = [details.get("output_artifact_id"), metadata.get("artifact_id") if isinstance(metadata, Mapping) else None]
    ids: list[str] = []
    for value in named:
        # Anything that is not an id is kept as text, which names no artifact, so a caller with limits is not shown it.
        if value is not None and str(value) not in ids:
            ids.append(str(value))
    return ids
