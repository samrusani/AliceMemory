from __future__ import annotations

import argparse
from collections.abc import Callable as _Callable
import functools as _functools
from alicebot_api.vnext_agent_keys import AgentKeyValidationError, create_agent_key
from alicebot_api.vnext_repositories import JsonObject
from .constants import logger
from .models import CLIContext
from .shared import _json_dumps, _vnext_store_context


def _reason_to_the_owner(
    handler: _Callable[[CLIContext, argparse.Namespace], str],
) -> _Callable[[CLIContext, argparse.Namespace], str]:
    """Log why a key command was refused at warning level, so the person who ran it reads it on stderr.

    The command answers every refusal with the same public ``invalid_request`` error, which names no reason, and the
    runner logs the exception only at debug level. The reasons of these two commands are fixed words and the names the
    person typed, so they are safe to print: a reserved project name, a blank project scope, an unknown key.
    """

    @_functools.wraps(handler)
    def run(ctx: CLIContext, args: argparse.Namespace) -> str:
        try:
            return handler(ctx, args)
        except AgentKeyValidationError as exc:
            logger.warning("%s", exc)
            raise

    return run


def _agent_key_public_record(record: dict[str, object]) -> JsonObject:
    return {
        "id": str(record.get("id")),
        "key_prefix": record.get("key_prefix"),
        "agent_id": record.get("agent_id"),
        "permission_profile": record.get("permission_profile"),
        "project_scope": record.get("project_scope"),
        "label": record.get("label"),
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
        "revoked_at": record.get("revoked_at"),
        "revoked": record.get("revoked_at") is not None,
    }


@_reason_to_the_owner
def _run_agent_keys_create(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        record, raw_key = create_agent_key(
            store,
            user_id=ctx.user_id,
            agent_id=args.agent_id,
            permission_profile=args.profile,
            label=args.label,
            project_scope=args.project_scope,
        )
    return _json_dumps(
        {
            "status": "created",
            "key": _agent_key_public_record(record),
            "raw_key": raw_key,
            "warning": (
                "Store this key now. It is shown exactly once; only its sha256 hash is persisted. "
                "Pass it to agent HTTP calls as 'Authorization: Bearer <raw_key>'."
            ),
        }
    )


def _run_agent_keys_list(ctx: CLIContext, args: argparse.Namespace) -> str:
    with _vnext_store_context(ctx) as store:
        records = store.list_agent_api_keys(limit=args.limit)
    items = [_agent_key_public_record(record) for record in records]
    return _json_dumps({"items": items, "count": len(items), "order": ["created_at_desc", "id_desc"]})


@_reason_to_the_owner
def _run_agent_keys_revoke(ctx: CLIContext, args: argparse.Namespace) -> str:
    selector = args.key.strip()
    if not selector:
        raise AgentKeyValidationError("a key prefix or key id is required")
    with _vnext_store_context(ctx) as store:
        records = store.list_agent_api_keys(limit=200)
        matches = [
            record
            for record in records
            if str(record.get("id")) == selector or str(record.get("key_prefix") or "") == selector
        ]
        if not matches:
            raise AgentKeyValidationError(f"no agent API key matches '{selector}'")
        if len(matches) > 1:
            raise AgentKeyValidationError(f"key prefix '{selector}' matches multiple keys; revoke by key id instead")
        revoked = store.revoke_agent_api_key(key_id=str(matches[0]["id"]))
        if revoked is None:
            raise AgentKeyValidationError(f"agent API key '{selector}' is already revoked")
    return _json_dumps({"status": "revoked", "key": _agent_key_public_record(revoked)})
