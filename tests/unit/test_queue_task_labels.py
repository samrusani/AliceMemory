"""A caller with limits claims only the queued tasks whose labels it may read.

``claimable_task_labels`` turns the read fence of a caller into the (domain, sensitivity) pairs the claim statement
filters on. It must give the pairs that the fence admits and no others, because the workspace screen lists tasks with
the same fence (``readable_own_label_rows``) and the claim must not reach a task that screen does not show.
"""
from __future__ import annotations

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, VNEXT_DOMAINS, AgentIdentity
from alicebot_api.vnext_label_guard import readable_own_label_rows
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService, claimable_task_labels
from tests.unit.test_vnext_queue import InMemoryVNextQueueStore

TRUSTED_LEVELS = {"public", "internal", "private", "unknown"}


def _identity(profile: str, *, scope: tuple[str, ...] = (), locked: bool = False) -> AgentIdentity:
    return AgentIdentity(agent_id="agent", permission_profile=profile, project_scope=scope, project_scope_locked=locked)


def test_the_owner_and_an_unbound_admin_key_have_no_limits() -> None:
    assert claimable_task_labels(None) is None
    assert claimable_task_labels(_identity("admin_agent")) is None


def test_a_trusted_key_may_claim_every_domain_at_its_four_levels() -> None:
    labels = claimable_task_labels(_identity("trusted_local_agent"))
    assert labels == {(domain, level) for domain in VNEXT_DOMAINS for level in TRUSTED_LEVELS}
    assert not any(level in {"confidential", "highly_sensitive", "sacred", "regulated"} for _domain, level in labels)


def test_a_key_locked_to_a_project_may_claim_nothing() -> None:
    # A queued task names no project the binding could hold it to.
    assert claimable_task_labels(_identity("trusted_local_agent", scope=("alpha",), locked=True)) == frozenset()
    assert claimable_task_labels(_identity("admin_agent", scope=("alpha",), locked=True)) == frozenset()


@pytest.mark.parametrize(
    "identity",
    [
        _identity("trusted_local_agent"),
        _identity("read_only_agent"),
        _identity("memory_proposal_agent"),
        _identity("project_scoped_agent", scope=("alpha",)),
        _identity("trusted_local_agent", scope=("alpha",), locked=True),
        _identity("admin_agent", scope=("alpha",), locked=True),
    ],
    ids=lambda identity: f"{identity.permission_profile}-{'locked' if identity.project_scope_locked else 'bound' if identity.project_scope else 'unbound'}",
)
def test_the_claimable_pairs_are_the_pairs_the_workspace_task_list_admits(identity: AgentIdentity) -> None:
    labels = claimable_task_labels(identity)
    assert labels is not None
    for domain in VNEXT_DOMAINS:
        for level in ALL_SENSITIVITY:
            listed = bool(readable_own_label_rows(identity, [{"domain": domain, "sensitivity": level}]))
            assert ((domain, level) in labels) == listed, (domain, level)


class _LabelledQueueStore(InMemoryVNextQueueStore):
    """Claims like the real store: the oldest pending task, within ``readable_labels`` when it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.claims: list[dict[str, object]] = []

    def claim_next_task(self, **kwargs):  # type: ignore[override]
        self.claims.append(kwargs)
        labels = kwargs.get("readable_labels")
        for task in self.tasks:
            if task["status"] != "pending":
                continue
            if labels is not None and (task.get("domain"), task.get("sensitivity")) not in labels:
                continue
            task["status"] = "running"
            return task
        return None


def _enqueue(service: VNextQueueService, title: str, sensitivity: str) -> None:
    service.enqueue_task(
        QueueTaskRequest(title=title, task_type="summarize", instructions=title, domain="project", sensitivity=sensitivity)
    )


def test_a_caller_with_no_limits_asks_the_store_for_the_next_task_as_before() -> None:
    store = _LabelledQueueStore()
    service = VNextQueueService(store)
    _enqueue(service, "older confidential", "confidential")

    assert service.process_next_task().task_id == "task-1"
    assert store.claims == [{}]


def test_a_caller_with_limits_claims_the_next_task_it_may_read_and_leaves_the_rest() -> None:
    store = _LabelledQueueStore()
    service = VNextQueueService(store)
    _enqueue(service, "older confidential", "confidential")
    _enqueue(service, "newer public", "public")
    labels = claimable_task_labels(_identity("trusted_local_agent"))

    first = service.process_next_task(readable_labels=labels)
    second = service.process_next_task(readable_labels=labels)

    assert (first.status, first.task_id) == ("completed", "task-2")
    assert second.to_record() == {
        "status": "idle", "task_id": None, "artifact_id": None, "error_code": None, "error_message": None,
    }
    assert [task["status"] for task in store.tasks] == ["pending", "completed"]
    assert [claim["readable_labels"] for claim in store.claims] == [labels, labels]
    assert store.events[-1]["event_type"] == "queue.worker_idle"


def test_a_caller_with_no_readable_label_gets_idle_and_changes_no_task() -> None:
    store = _LabelledQueueStore()
    service = VNextQueueService(store)
    _enqueue(service, "public", "public")

    assert service.process_next_task(readable_labels=frozenset()).status == "idle"
    assert store.tasks[0]["status"] == "pending"
