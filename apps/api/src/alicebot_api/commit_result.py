"""The compact ``alice_memory_commit`` result.

Unreleased (on main, not in v0.20.0). Search-quality spec, section 7.

``alice_memory_commit`` returns the whole stored memory row and the policy
decision three times, about 3.7 KB for a one-sentence fact. An agent that saves
a note needs the id, the outcome, the receipt, and when the write was held, why
and under which confirmation id. This module builds that view from the full
result, and nothing else.

The rules, each pinned by a test in ``tests/unit/test_compact_commit_result.py``:

* The view is built from the full result by selecting keys. It never reads the
  store, so it can show no row, field or value the full result did not carry.
  ``compact_commit_result`` has one parameter, the full result.
* The top level is a denylist. ``policy_decision`` is dropped after the keys an
  agent acts on are lifted out of it, and ``memory`` is replaced by a view. Every
  other top-level key passes through unchanged, so a key a later slice adds
  (``saved_to``, ``will_save_to``, ``scope_requested``) survives with no edit here.
* The memory view is an allowlist of fields that are not storage detail. It
  keeps ``project_scope``, ``project_id``, ``supersedes`` and ``superseded_by``
  because they are the agent's receipt of where a note landed and what it
  replaced.
* The switch is read at the edge, in ``call_mcp_tool``, and passed in as a
  mapping. This module never reads the process environment.

The legacy alias ``alice_vnext_commit_memory`` shares the handler and keeps the
full result in both modes, and so do the HTTP and CLI commit paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from alicebot_api.store import JsonObject, JsonValue

COMMIT_RESULT_ENV = "ALICE_MCP_COMMIT_RESULT"
#: The one tool name whose result is compacted. The legacy alias is not on this list.
COMMIT_RESULT_TOOL = "alice_memory_commit"

CommitResultMode = Literal["compact", "full"]

#: What an unset, empty or unrecognised ``ALICE_MCP_COMMIT_RESULT`` means. It is
#: ``full`` until the release that turns the compact result on, so main returns
#: the bytes v0.20.0 returned. That release changes this one value.
BUILD_DEFAULT_COMMIT_RESULT: CommitResultMode = "full"

#: Keys of the full result's ``policy_decision`` that an agent acts on. They are
#: copied up to the top level, and only when the top level does not already hold
#: the key, so a lifted value never replaces a value the full result had there.
LIFTED_POLICY_KEYS = ("reason", "reasons", "requires_confirmation", "requires_dashboard_review")

#: Fields of the saved memory the view keeps. A field the full memory does not
#: hold is not added.
MEMORY_VIEW_KEYS = (
    "id",
    "status",
    "title",
    "canonical_text",
    "memory_type",
    "domain",
    "sensitivity",
    "confidence",
    "created_at",
    "created_by_agent_id",
    "project_scope",
    "project_id",
    "supersedes",
    "superseded_by",
)


def parse_commit_result_mode(text: object) -> CommitResultMode | None:
    """``compact`` or ``full`` (any case, surrounding space ignored), else ``None``."""

    if not isinstance(text, str):
        return None
    word = text.strip().lower()
    if word == "compact":
        return "compact"
    if word == "full":
        return "full"
    return None


def commit_result_mode(environ: Mapping[str, str]) -> CommitResultMode:
    """The mode the environment asks for, or the build default when it asks for none."""

    return parse_commit_result_mode(environ.get(COMMIT_RESULT_ENV)) or BUILD_DEFAULT_COMMIT_RESULT


def compact_commit_result(full: Mapping[str, JsonValue]) -> JsonObject:
    """The compact view of one full ``alice_memory_commit`` result.

    Pure: nothing is read except ``full``, and every value in the answer is a
    value of ``full``. A result that holds no ``memory`` (a rejected write) or no
    ``policy_decision`` (the finish of a confirmation) is compacted without them.
    """

    view: JsonObject = {}
    for key, value in full.items():
        if key == "policy_decision":
            continue
        if key == "memory":
            view[key] = _memory_view(value)
            continue
        view[key] = value
    decision = full.get("policy_decision")
    if isinstance(decision, Mapping):
        for key in LIFTED_POLICY_KEYS:
            if key in decision and key not in view:
                view[key] = decision[key]
    return view


def _memory_view(memory: JsonValue) -> JsonValue:
    if not isinstance(memory, Mapping):
        return memory
    return {key: memory[key] for key in MEMORY_VIEW_KEYS if key in memory}


__all__ = [
    "BUILD_DEFAULT_COMMIT_RESULT",
    "COMMIT_RESULT_ENV",
    "COMMIT_RESULT_TOOL",
    "LIFTED_POLICY_KEYS",
    "MEMORY_VIEW_KEYS",
    "CommitResultMode",
    "commit_result_mode",
    "compact_commit_result",
    "parse_commit_result_mode",
]
