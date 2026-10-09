"""One probe per /v0/vnext route, aimed at the hidden rows of an ``operator_route_vault.Vault``.

A probe is a list of calls. Every call asks as much as the route lets a caller ask: the hidden id where the route names a
row, every sensitivity where the route takes a filter, the text of a hidden row where the route deduplicates. The sweep
test runs each call with an unbound trusted key and requires that the answer carries none of the vault's hidden texts and
that no hidden row changed.

The table is keyed by (method, path template) and the sweep test requires its keys to be exactly the routes the mounted
application has under /v0/vnext, so a new route fails the test until someone writes its probe.
"""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from tests.integration.operator_route_vault import StubVault, Vault

ALL_SENSITIVITY = ["public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown"]


@dataclass(frozen=True)
class Call:
    name: str
    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, object] = field(default_factory=dict)
    body: dict[str, object] | None = None
    # Sentinel names an unbound admin key must see in the answer: the proof that the call reaches the row at all.
    admin_sees: tuple[str, ...] = ()
    # Sentinel names the trusted key must still see: the proof that the route still answers a caller with limits.
    trusted_sees: tuple[str, ...] = ()
    # Status codes the trusted key may get. Empty means any status below 500.
    statuses: tuple[int, ...] = ()
    # True when the call, if it were admitted, would change a row the sweep then compares.
    mutates: bool = False


Probe = Callable[[Vault], list[Call]]


def _ids(v: Vault, **names: str) -> dict[str, str]:
    return {key: v.ids[value] for key, value in names.items()}


def _hidden_everything(v: Vault) -> dict[str, object]:
    return {"sensitivity_allowed": list(ALL_SENSITIVITY)}


def _scope() -> dict[str, object]:
    return {}


PROBES: dict[tuple[str, str], Probe] = {}


def probe(method: str, path: str):
    def register(fn: Probe) -> Probe:
        PROBES[(method, path)] = fn
        return fn

    return register


# -- sources ------------------------------------------------------------------------------------------------------


@probe("POST", "/v0/vnext/sources")
def _(v):
    return [
        Call("create", body={"raw_text": "a new note the key wrote", "title": "new note", "domain": "project", "sensitivity": "public"}),
        Call(
            "duplicate_of_hidden",
            body={"raw_text": v.text("dedupe_source-raw"), "title": "again", "domain": "project", "sensitivity": "public"},
        ),
    ]


@probe("GET", "/v0/vnext/sources/{source_id}")
def _(v):
    return [
        Call("hidden", path=_ids(v, source_id="source_hidden"), admin_sees=("source_hidden-title", "source_hidden-raw"), statuses=(404,)),
        Call("shown", path=_ids(v, source_id="source_shown"), trusted_sees=("source_shown-title",), statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/sources/{source_id}/review")
def _(v):
    hidden = _ids(v, source_id="source_hidden")
    return [
        Call("review_hidden", path=hidden, body={"action": "review", "review_note": "checked"}, mutates=True, statuses=(404,)),
        Call("update_hidden", path=hidden, body={"action": "update", "title": "RENAMED"}, mutates=True, statuses=(404,)),
        Call("archive_hidden", path=hidden, body={"action": "archive"}, mutates=True, statuses=(404,)),
        Call(
            "assign_hidden",
            path=hidden,
            body={"action": "assign_project", "project_id": v.ids["project_shown"], "confirm_label_hide": True},
            mutates=True,
            statuses=(404,),
        ),
        Call("review_shown_trace", path=_ids(v, source_id="source_shown"), body={"action": "review"}, statuses=(200,)),
    ]


@probe("DELETE", "/v0/vnext/sources/{source_id}")
def _(v):
    return [Call("delete_hidden", path=_ids(v, source_id="source_hidden"), mutates=True, statuses=(404,))]


@probe("POST", "/v0/vnext/sources/{source_id}/regenerate")
def _(v):
    return [Call("regenerate_hidden", path=_ids(v, source_id="source_hidden"), body={}, mutates=True, statuses=(403,))]


@probe("GET", "/v0/vnext/traces/sources/{source_id}")
def _(v):
    return [
        Call("hidden", path=_ids(v, source_id="source_hidden"), statuses=(200, 404)),
        Call("shown", path=_ids(v, source_id="source_shown"), statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/agents/ingest-output")
def _(v):
    return [
        Call(
            "cites_hidden_source",
            body={
                "agent_id": v.agent_ids["trusted_local_agent"],
                "title": "an agent output",
                "content": "an output that cites a source",
                "output_type": "summary",
                "source_refs": [v.ids["source_hidden"]],
            },
        ),
    ]


# -- memories -----------------------------------------------------------------------------------------------------


@probe("GET", "/v0/vnext/memories/recent-commits")
def _(v):
    return [
        Call(
            "default",
            admin_sees=("commit_hidden-text", "commit_confirmed_hidden-text", "commit_derived-text", "commit_shown-text"),
            trusted_sees=("commit_shown-text",),
            statuses=(200,),
        ),
        Call("long", query={"limit": 100}, admin_sees=("commit_hidden-text",), trusted_sees=("commit_shown-text",), statuses=(200,)),
        # The newest commit is hidden from the trusted key, so the one it may read is found further back.
        Call("one", query={"limit": 1}, admin_sees=("dedupe_memory-title",), trusted_sees=("commit_shown-text",), statuses=(200,)),
    ]


@probe("GET", "/v0/vnext/memories/{memory_id}/audit")
def _(v):
    return [
        Call("hidden", path=_ids(v, memory_id="memory_hidden"), admin_sees=("memory_hidden-text",), statuses=(404,)),
        Call("derived", path=_ids(v, memory_id="memory_derived"), admin_sees=("memory_derived-text",), statuses=(404,)),
        Call("commit", path=_ids(v, memory_id="commit_hidden"), admin_sees=("commit_hidden-text",), statuses=(404,)),
        Call("shown", path=_ids(v, memory_id="memory_shown"), trusted_sees=("memory_shown-text",), statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/memories/{memory_id}/review")
def _(v):
    return [
        Call("hidden_edit", path=_ids(v, memory_id="memory_hidden"), body={"action": "edit", "title": "RENAMED"}, mutates=True),
        Call("derived_accept", path=_ids(v, memory_id="memory_derived"), body={"action": "accept"}, mutates=True),
        Call("candidate_reject", path=_ids(v, memory_id="candidate_hidden"), body={"action": "reject"}, mutates=True),
    ]


@probe("POST", "/v0/vnext/memory-proposals")
def _(v):
    return [
        Call(
            "cites_hidden",
            body={
                "title": "a proposal",
                "canonical_text": "a proposal that cites a source",
                "source_refs": [v.ids["source_hidden"]],
                "domain": "project",
                "sensitivity": "public",
            },
        ),
        Call(
            "text_of_hidden",
            body={"title": "again", "canonical_text": v.text("dedupe_memory-text"), "domain": "project", "sensitivity": "public"},
        ),
    ]


@probe("POST", "/v0/vnext/memories/commit")
def _(v):
    return [
        Call(
            "text_of_hidden",
            body={
                "title": "again",
                "canonical_text": v.text("dedupe_memory-text"),
                "domain": "project",
                "sensitivity": "public",
                "confidence": 0.99,
            },
        ),
        Call(
            "cites_hidden",
            body={
                "title": "cites",
                "canonical_text": "a commit that cites a hidden source",
                "domain": "project",
                "sensitivity": "public",
                "confidence": 0.99,
                "source_refs": [v.ids["source_hidden"]],
            },
        ),
    ]


def _memory_call(v: Vault, name: str, memory: str, **extra) -> Call:
    return Call(name, body={"memory_id": v.ids[memory], **extra}, mutates=True)


@probe("POST", "/v0/vnext/memories/confirm")
def _(v):
    return [
        Call("pending_hidden", body={"confirmation_id": v.ids["commit_hidden_confirmation"], "action": "confirm"}, mutates=True),
        Call("reject_hidden", body={"confirmation_id": v.ids["commit_hidden_confirmation"], "action": "reject"}, mutates=True),
    ]


@probe("POST", "/v0/vnext/memories/undo")
def _(v):
    return [_memory_call(v, "hidden", "commit_confirmed_hidden", reason="undo"), _memory_call(v, "derived", "memory_derived", reason="undo")]


@probe("POST", "/v0/vnext/memories/correct")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", canonical_text="a correction", reason="fix")]


@probe("POST", "/v0/vnext/memories/forget")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", reason="forget"), _memory_call(v, "derived", "memory_derived", reason="forget")]


@probe("POST", "/v0/vnext/memories/expire")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", reason="expire")]


@probe("POST", "/v0/vnext/memories/unexpire")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", reason="unexpire")]


@probe("POST", "/v0/vnext/memories/accept-consolidation")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", reason="accept")]


@probe("POST", "/v0/vnext/memories/redact")
def _(v):
    return [_memory_call(v, "hidden", "memory_hidden", reason="redact")]


# -- artifacts ----------------------------------------------------------------------------------------------------


@probe("GET", "/v0/vnext/artifacts")
def _(v):
    return [
        Call("default", admin_sees=("artifact_hidden-title", "artifact_derived-title"), trusted_sees=("artifact_shown-title",), statuses=(200,)),
        Call("daily", query={"artifact_type": "daily_brief", "limit": 100}, admin_sees=("artifact_hidden-title",), statuses=(200,)),
    ]


@probe("GET", "/v0/vnext/artifacts/{artifact_id}")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), admin_sees=("artifact_hidden-title",), statuses=(404,)),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), admin_sees=("artifact_derived-title",), statuses=(404,)),
        Call("shown", path=_ids(v, artifact_id="artifact_shown"), trusted_sees=("artifact_shown-title",), statuses=(200,)),
    ]


@probe("GET", "/v0/vnext/traces/artifacts/{artifact_id}")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), admin_sees=("artifact_hidden-title",), statuses=(404,)),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), admin_sees=("artifact_derived-title",), statuses=(404,)),
    ]


@probe("POST", "/v0/vnext/artifacts/{artifact_id}/review")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), body={"action": "accept"}, mutates=True),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), body={"action": "accept"}, mutates=True),
    ]


@probe("POST", "/v0/vnext/artifacts/{artifact_id}/export")
def _(v):
    out = tempfile.mkdtemp(prefix="sweep-export-")
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), body={"output_dir": out}),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), body={"output_dir": out}),
    ]


@probe("POST", "/v0/vnext/artifacts/{artifact_id}/insight-feedback")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), body={"useful_insight": "yes"}, mutates=True),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), body={"useful_insight": "yes"}, mutates=True),
    ]


@probe("POST", "/v0/vnext/artifacts/{artifact_id}/quality-ratings")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), body={"reviewer_id": "r", "comments": "c"}, mutates=True),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), body={"reviewer_id": "r", "comments": "c"}, mutates=True),
    ]


@probe("GET", "/v0/vnext/quality-evals")
def _(v):
    return [
        Call("default", admin_sees=("artifact_hidden-rating", "artifact_derived-rating"), trusted_sees=("artifact_shown-rating",), statuses=(200,)),
        Call("of_hidden", query={"artifact_id": v.ids["artifact_hidden"], "limit": 100}, admin_sees=("artifact_hidden-rating",), statuses=(200, 404)),
    ]


_BRIEF_SEEN = {
    "daily-brief": ("source_hidden-title", "loop_hidden-title"),
    "weekly-synthesis": ("source_hidden-title", "commit_confirmed_hidden-text"),
}


def _generate(v: Vault, name: str) -> list[Call]:
    seen = _BRIEF_SEEN.get(name, ())
    return [
        Call("default", body={}),
        Call("everything", body={"scope": {}, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY)}}, admin_sees=seen),
    ]


for _name in ("daily-brief", "weekly-synthesis", "connections", "contradictions"):
    PROBES[("POST", f"/v0/vnext/artifacts/generate/{_name}")] = lambda v, _name=_name: _generate(v, _name)


@probe("POST", "/v0/vnext/projects/update-candidates")
def _(v):
    return [Call("default", body={}), Call("everything", body={"scope": {}, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY)}})]


@probe("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review")
def _(v):
    return [
        Call("hidden", path=_ids(v, artifact_id="artifact_hidden"), body={"action": "accept"}, mutates=True),
        Call("derived", path=_ids(v, artifact_id="artifact_derived"), body={"action": "accept"}, mutates=True),
    ]


# -- projects, loops, beliefs, graph -------------------------------------------------------------------------------


@probe("POST", "/v0/vnext/projects")
def _(v):
    return [
        Call("new", body={"name": "a new project", "domain": "project", "sensitivity": "public"}),
        Call(
            "slug_of_hidden",
            body={"name": "again", "slug": f"{v.tag}-dedupe-project".lower(), "domain": "project", "sensitivity": "public"},
        ),
    ]


@probe("GET", "/v0/vnext/projects")
def _(v):
    return [
        Call("default", admin_sees=("project_hidden-name",), trusted_sees=("project_shown-name",), statuses=(200,)),
        Call("all", query={"status": "", "limit": 100}, statuses=(200, 422)),
    ]


@probe("GET", "/v0/vnext/projects/{project_id}/dashboard")
def _(v):
    return [
        Call("hidden", path=_ids(v, project_id="project_hidden"), admin_sees=("project_hidden-name",), statuses=(404,)),
        Call("shown", path=_ids(v, project_id="project_shown"), trusted_sees=("project_shown-name",), statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/open-loops")
def _(v):
    return [
        Call("on_hidden_memory", body={"title": "a loop", "memory_id": v.ids["memory_hidden"], "domain": "project", "sensitivity": "public"}),
        Call("on_hidden_source", body={"title": "a loop", "source_id": v.ids["source_hidden"], "domain": "project", "sensitivity": "public"}),
        Call("on_hidden_project", body={"title": "a loop", "project_id": v.ids["project_hidden"], "domain": "project", "sensitivity": "public"}),
    ]


@probe("POST", "/v0/vnext/open-loops/{loop_id}/review")
def _(v):
    return [
        Call("hidden", path=_ids(v, loop_id="loop_hidden"), body={"action": "close"}, mutates=True),
        Call("derived", path=_ids(v, loop_id="loop_derived"), body={"action": "close"}, mutates=True),
    ]


@probe("POST", "/v0/vnext/open-loops/extract")
def _(v):
    return [
        Call("default", body={}, admin_sees=("source_shown-todo",)),
        Call(
            "everything",
            body={"scope": {}, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "max_items": 50}},
            admin_sees=("source_hidden-todo", "capture_hidden-todo"),
            trusted_sees=("source_shown-todo",),
        ),
    ]


@probe("GET", "/v0/vnext/beliefs/{belief_id}/state")
def _(v):
    return [
        Call("hidden", path=_ids(v, belief_id="belief_hidden"), admin_sees=("belief_hidden-claim",), statuses=(404,)),
        Call("shown", path=_ids(v, belief_id="belief_shown"), statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/beliefs/{belief_id}/review")
def _(v):
    return [Call("hidden", path=_ids(v, belief_id="belief_hidden"), body={"action": "accept"}, mutates=True)]


@probe("GET", "/v0/vnext/graph/neighborhood/{target_id}")
def _(v):
    return [
        Call("hidden", path=_ids(v, target_id="memory_hidden"), admin_sees=("edge_hidden-explanation",)),
        Call("shown_next_to_hidden", path=_ids(v, target_id="memory_shown"), admin_sees=("edge_hidden-explanation",)),
        Call("hidden_source", path=_ids(v, target_id="source_hidden")),
    ]


@probe("POST", "/v0/vnext/graph/edges/{edge_id}/review")
def _(v):
    return [Call("hidden", path=_ids(v, edge_id="edge_hidden"), body={"action": "accept"}, mutates=True)]


# -- screens ------------------------------------------------------------------------------------------------------


@probe("GET", "/v0/vnext/workspace")
def _(v):
    return [
        Call(
            "default",
            # The screen lists rows at public, internal, private and unknown for everyone. What the owner sees beyond
            # that is a task, the charter and a row stored at a level that its inputs raise.
            admin_sees=(
                "artifact_derived-title", "task_hidden-title", "task_hidden-instructions",
                "charter-body", "charter-owner", "charter-priority", "charter-project", "charter-rule",
                # The connector health block names the file of the last import, which is confidential.
                "synced-file",
            ),
            trusted_sees=("source_shown-title", "task_shown-title"),
            statuses=(200,),
        )
    ]


@probe("GET", "/v0/vnext/context-tree")
def _(v):
    return [
        Call("default", statuses=(200,)),
        Call(
            "every_sensitivity",
            query={"sensitivity_allowed": list(ALL_SENSITIVITY), "limit": 50},
            admin_sees=("artifact_hidden-title", "project_hidden-name", "loop_hidden-title"),
            statuses=(200,),
        ),
        Call(
            "confidential_only",
            query={"sensitivity_allowed": ["confidential"], "limit": 50},
            admin_sees=("artifact_hidden-title", "project_hidden-name"),
            statuses=(200,),
        ),
    ]


@probe("GET", "/v0/vnext/dogfooding")
def _(v):
    return [Call("default", admin_sees=("synced-file",), statuses=(200,))]


@probe("GET", "/v0/vnext/doctor")
def _(v):
    return [
        Call("default", admin_sees=("synced-file",), statuses=(200,)),
        Call("ci", query={"ci": "true"}, statuses=(200,)),
    ]


@probe("POST", "/v0/vnext/doctor/run")
def _(v):
    return [
        Call("default", body={}, admin_sees=("synced-file",), statuses=(200,)),
        Call("fix_safe", body={"fix_safe": True, "ci": True}),
    ]


@probe("GET", "/v0/vnext/agents/policy-telemetry")
def _(v):
    return [Call("default", statuses=(200,)), Call("long", query={"limit": 200}, statuses=(200, 422))]


@probe("POST", "/v0/vnext/context-packs")
def _(v):
    return [
        Call("asks_for_it", body={"query": v.text("dedupe_memory-text"), "options": {"sensitivity_allowed": list(ALL_SENSITIVITY)}}),
        Call(
            "by_word",
            body={"query": "SENTINEL", "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "limit": 50}},
            admin_sees=("memory_hidden-title", "source_hidden-title", "loop_hidden-title"),
        ),
    ]


# -- queue and scheduler ------------------------------------------------------------------------------------------


@probe("POST", "/v0/vnext/queue/tasks")
def _(v):
    return [
        Call(
            "new",
            body={"title": "a task", "task_type": "summarize", "instructions": "summarize the vault", "domain": "project", "sensitivity": "public"},
        )
    ]


@probe("POST", "/v0/vnext/queue/process-next")
def _(v):
    # The claim statement of the queue is ambiguous in PostgreSQL, so the route answers 500 to every caller today. The
    # calls stay, so the day it works the oldest pending task (the hidden one) is claimed by the trusted key here and
    # the answer is read for what it carries.
    return [Call("first", body={}, mutates=True), Call("second", body={}, mutates=True), Call("third", body={}, mutates=True)]


@probe("GET", "/v0/vnext/scheduler/status")
def _(v):
    return [Call("default", statuses=(200,))]


@probe("GET", "/v0/vnext/scheduler/runs")
def _(v):
    return [Call("default", statuses=(200,)), Call("long", query={"limit": 200}, statuses=(200,))]


@probe("GET", "/v0/vnext/scheduler/failures")
def _(v):
    return [Call("default", statuses=(200,)), Call("long", query={"limit": 200}, statuses=(200,))]


@probe("POST", "/v0/vnext/scheduler/run-due")
def _(v):
    return [Call("default", body={})]


@probe("POST", "/v0/vnext/scheduler/pause")
def _(v):
    return [Call("default", body={})]


@probe("POST", "/v0/vnext/scheduler/resume")
def _(v):
    return [Call("default", body={})]


@probe("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}")
def _(v):
    return [Call("daily", path={"workflow_type": "daily_brief"}, body={"enabled": True})]


@probe("POST", "/v0/vnext/scheduler/workflows/{workflow_type}/run-now")
def _(v):
    return [
        Call("daily", path={"workflow_type": "daily_brief"}, body={}),
        Call("weekly", path={"workflow_type": "weekly_synthesis"}, body={}),
        Call("consolidation", path={"workflow_type": "memory_consolidation"}, body={}),
        Call("open_loop_review", path={"workflow_type": "open_loop_review"}, body={}),
    ]


@probe("GET", "/v0/vnext/settings/brain-charter")
def _(v):
    return [
        Call(
            "default",
            admin_sees=("charter-body", "charter-owner", "charter-priority", "charter-project", "charter-rule"),
            statuses=(200,),
        )
    ]


@probe("PUT", "/v0/vnext/settings/brain-charter")
def _(v):
    # The save replaces the whole charter, and the stored one is confidential.
    return [Call("overwrite", body={"content_markdown": "a charter the key wrote"}, mutates=True, statuses=(403,))]


# -- connectors ---------------------------------------------------------------------------------------------------


@probe("GET", "/v0/vnext/connectors")
def _(v):
    return [Call("default", admin_sees=("synced-file",), statuses=(200,))]


@probe("GET", "/v0/vnext/connectors/health")
def _(v):
    return [Call("default", admin_sees=("synced-file",), statuses=(200,))]


@probe("GET", "/v0/vnext/connectors/{connector_name}/status")
def _(v):
    return [
        Call(
            "local_folder",
            path={"connector_name": "local_folder"},
            admin_sees=("capture_hidden-title", "capture_hidden-raw", "synced-title", "synced-file"),
            trusted_sees=("capture_shown-title",),
            statuses=(200,),
        ),
        Call("telegram", path={"connector_name": "telegram"}, statuses=(200,)),
    ]


@probe("PATCH", "/v0/vnext/connectors/{connector_name}/config")
def _(v):
    return [Call("local_folder", path={"connector_name": "local_folder"}, body={"enabled": True})]


@probe("POST", "/v0/vnext/connectors/{connector_name}/sync")
def _(v):
    return [
        Call(
            "duplicate_of_hidden",
            path={"connector_name": "local_folder"},
            body={"items": [{"path": "/sweep/again.md", "title": "again", "text": v.text("dedupe_source-raw")}]},
        )
    ]


@probe("POST", "/v0/vnext/connectors/telegram/sync")
def _(v):
    return [Call("one_update", body={"updates": [{"update_id": 1}], "allowed_chat_ids": ["1"]})]


@probe("POST", "/v0/vnext/connectors/local-folder/sync")
def _(v):
    return [Call("configured_paths", body={})]


@probe("POST", "/v0/vnext/connectors/browser-clipper/capabilities")
def _(v):
    return [Call("issue", body={"origin": "https://example.invalid"})]


@probe("POST", "/v0/vnext/connectors/browser-clipper/capture")
def _(v):
    return [Call("no_capability", body={"url": "https://example.invalid/page", "page_text": v.text("dedupe_source-raw")})]


# -- what the table says about itself -------------------------------------------------------------------------------

#: Routes whose probe fails today for a reason this change does not own. Each is run, and the run must still fail
#: (strict), so the day the owning change lands the test fails and this entry is deleted.
PENDING: dict[tuple[str, str], str] = {
    ("GET", "/v0/vnext/graph/neighborhood/{target_id}"): (
        "the neighborhood lists an edge whose far end the key may not read, with the explanation it was made with; the "
        "redaction containment change judges the rows an edge joins"
    ),
}


def control_routes() -> list[tuple[str, str]]:
    """The routes with a call that names sentinels the unbound admin key and the owner must still be shown."""

    stub = StubVault()
    return sorted(route for route, build in PROBES.items() if any(call.admin_sees for call in build(stub)))  # type: ignore[arg-type]
