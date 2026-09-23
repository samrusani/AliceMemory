#!/usr/bin/env python3
"""Alice memory tools shaped for the OpenAI Agents SDK's ``@function_tool``.

This example defines two plain, typed Python functions —
``alice_capture_memory`` and ``alice_recall_memories`` — in exactly the shape
the OpenAI Agents SDK expects from a function tool, without importing the SDK.
Drop them into an Agents SDK project and wrap each with ``@function_tool``; no
adapter layer is needed.

Function-tool contract (validated against the shape documented for
``openai-agents==0.1.0``; the SDK is deliberately NOT imported here, and this
repo adds no dependency on it):

- The decorator builds the tool's JSON schema from the function signature:
  every parameter needs a type hint that maps to JSON (``str``, ``int``,
  ``float``, ``bool``, ``str | None``, ...). Defaults mark parameters
  optional.
- The docstring's first paragraph becomes the tool description; google-style
  ``Args:`` entries become per-parameter descriptions.
- The return value is fed back to the model. Returning a compact JSON string
  keeps the payload deterministic and model-readable.
- Errors: raising an exception fails the tool call (the SDK surfaces
  ``failure_error_function`` hooks); these tools raise
  ``AliceMemoryToolError`` with a ``status_code`` attribute on HTTP failures
  so hosts can branch on auth errors (401/403) versus transport errors.

Usage with the SDK installed (commented so this file stays import-free)::

    # from agents import Agent, function_tool
    #
    # capture_tool = function_tool(alice_capture_memory)
    # recall_tool = function_tool(alice_recall_memories)
    #
    # agent = Agent(
    #     name="alice-aware-assistant",
    #     instructions="Recall Alice context before acting; capture explicit facts.",
    #     tools=[capture_tool, recall_tool],
    # )

Authentication (real per-agent API keys, not headers invented for a demo):

1. Create a key once: ``alicebot agent keys create --agent-id my-agent
   --profile trusted_local_agent`` — the raw ``alice_sk_...`` key is printed
   exactly once.
2. Export it: ``export ALICE_AGENT_API_KEY="alice_sk_..."``. Every call sends
   it as ``Authorization: Bearer <key>``; once any active key exists, Alice
   rejects keyless ``/v0/vnext`` requests, and the key record (not the
   payload) decides ``agent_id`` and ``permission_profile``.
3. ``read_only_agent`` keys can recall but their captures are rejected with
   ``read_only_agent_cannot_write`` — never a silent write.

Environment:

- ``ALICE_BASE_URL``   — Alice API base URL (default ``http://127.0.0.1:8000``).
- ``ALICE_AGENT_API_KEY`` — raw agent API key (required).
- ``ALICE_USER_ID``    — local Alice operator UUID (required; tenancy boundary).

Demo mode (requires a running Alice API plus the env above)::

    python docs/examples/openai_agents_sdk_tool.py
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AliceMemoryToolError(RuntimeError):
    """Raised when Alice rejects or cannot serve a tool call.

    ``status_code`` is the HTTP status when the failure came from Alice
    (401/403 mean a missing, invalid, or insufficient agent API key) and
    ``None`` when Alice was unreachable.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AliceMemoryToolError(
            f"Missing {name}. Create a key with 'alicebot agent keys create' and export it "
            "along with ALICE_USER_ID before using the Alice memory tools."
        )
    return value


def _post_alice(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base_url = os.getenv("ALICE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = _required_env("ALICE_AGENT_API_KEY")
    request = Request(
        url=f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            # Real agent-key auth: the key record overrides any identity
            # claimed in the payload; escalation attempts are rejected (403).
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AliceMemoryToolError(
            f"Alice returned HTTP {exc.code} for {path}: {detail}",
            status_code=exc.code,
        ) from exc
    except URLError as exc:
        raise AliceMemoryToolError(f"Failed to reach Alice at {base_url}: {exc.reason}") from exc


def alice_capture_memory(
    text: str,
    title: str | None = None,
    domain: str = "professional",
    sensitivity: str = "internal",
) -> str:
    """Capture an explicit fact into Alice's long-term memory.

    Use this when the user states something worth remembering across
    sessions ("remember this", a decision, a stable preference). The commit
    is policy-checked and audited — never a silent write. The JSON result's
    ``status`` is one of ``committed``, ``confirmation_required``,
    ``review_required``, or ``rejected`` (with ``reasons``).

    Args:
        text: The canonical fact to remember, phrased as one standalone sentence.
        title: Optional short label for the memory; derived from the text when omitted.
        domain: Memory domain such as ``professional``, ``personal``, or ``project``.
        sensitivity: Sensitivity tier: ``public``, ``internal``, or ``private``.

    Returns:
        JSON string with ``status``, ``memory_id`` (when a row was written),
        and ``reasons`` (when the commit was not applied).
    """
    payload = _post_alice(
        "/v0/vnext/memories/commit",
        {
            "user_id": _required_env("ALICE_USER_ID"),
            "intent": "explicit_remember",
            "title": (title or text.strip().splitlines()[0][:120]),
            "canonical_text": text,
            "memory_type": "semantic",
            "domain": domain,
            "sensitivity": sensitivity,
            "confidence": 0.96,
            "source_type": "direct_user_instruction",
        },
    )
    memory = payload.get("memory") or {}
    return json.dumps(
        {
            "status": payload.get("status"),
            "memory_id": memory.get("id"),
            "memory_status": memory.get("status"),
            "reasons": payload.get("reasons") or [],
        },
        sort_keys=True,
    )


def alice_recall_memories(query: str, max_items: int = 5) -> str:
    """Recall the most relevant Alice memories for a query.

    Call this before planning or acting so the agent starts from the user's
    actual context instead of a blank slate. Results are policy-filtered by
    the authenticated agent key's permission profile and project scope.

    Args:
        query: Natural-language description of the context needed.
        max_items: Maximum number of memories to return (1-50).

    Returns:
        JSON string with ``framing``, ``context_pack_id``, and ``memories``.
        Each memory keeps the stored ``text`` byte for byte and carries
        ``writer``. ``framing`` is the one line that says those notes are
        data, not instructions.
    """
    payload = _post_alice(
        "/v0/vnext/context-packs",
        {
            "user_id": _required_env("ALICE_USER_ID"),
            "query": query,
            "options": {"max_items": max_items},
        },
    )
    memories = [
        {
            "id": str(item.get("id")),
            "title": item.get("title"),
            "memory_type": item.get("memory_type"),
            "text": item.get("canonical_text") or item.get("summary") or "",
            "writer": item.get("writer"),
        }
        for item in payload.get("relevant_memories") or []
    ]
    return json.dumps(
        {
            "framing": payload.get("framing"),
            "context_pack_id": payload.get("context_pack_id"),
            "memories": memories,
        },
        sort_keys=True,
    )


def _demo() -> int:
    """Exercise both tool functions against a live Alice API."""

    captured = json.loads(
        alice_capture_memory(
            "The OpenAI Agents SDK tool demo prefers concise context packs.",
            title="Agents SDK tool demo preference",
        )
    )
    print(json.dumps({"capture": captured}, indent=2, sort_keys=True))
    if captured["status"] not in {"committed", "confirmation_required", "review_required"}:
        print(f"Capture was not accepted: {captured}")
        return 1

    recalled = json.loads(alice_recall_memories("Agents SDK tool demo context pack preference"))
    print(json.dumps({"recall": recalled}, indent=2, sort_keys=True))
    if not recalled["memories"]:
        print("Recall returned no memories.")
        return 1

    print("SDK TOOL DEMO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
