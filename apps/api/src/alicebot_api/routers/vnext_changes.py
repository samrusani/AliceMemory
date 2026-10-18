"""POST /v0/vnext/changes: the what-changed read for one topic."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from alicebot_api.config import get_settings
from alicebot_api.db import user_connection
from alicebot_api.source_search_limits import SourceSearchQueryTooLarge
from alicebot_api.vnext_agent_control import (
    AgentPolicyBlockedError,
    append_policy_events,
    evaluate_agent_policy,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    agent_key_from_authorization,
    resolve_protected_agent_identity,
)
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_what_changed import what_changed

from ._vnext_shared import _vnext_agent_auth_error_response, _vnext_permission_response, _vnext_public_error_response

changes_router = APIRouter()


class WhatChangedBody(BaseModel):
    topic: str = ""


@changes_router.post("/v0/vnext/changes")
def post_vnext_changes(
    user_id: UUID,
    body: WhatChangedBody,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Recorded decision chains for one topic that this caller may read.

    The answer is the history found among at most 128 candidates. It gives no
    count and no gap marker. A topic with nothing the caller may read says that
    no recorded decision history was found and names alice_recall.
    """
    settings = get_settings()
    try:
        with user_connection(settings.database_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            identity = resolve_protected_agent_identity(
                store,
                user_id=user_id,
                raw_key=agent_key_from_authorization(authorization),
                payload={},
            )
            decision = evaluate_agent_policy(identity=identity, action="recent_decisions.lookup")
            append_policy_events(
                store,
                identity=identity,
                decision=decision,
                target_type="http_route",
                target_id="/v0/vnext/changes",
            )
            if decision.decision == "blocked":
                raise AgentPolicyBlockedError(decision)
            payload = what_changed(store, user_id=user_id, identity=identity, topic=body.topic)
    except AgentKeyAuthenticationError as exc:
        return _vnext_agent_auth_error_response(exc)
    except AgentPolicyBlockedError as exc:
        return _vnext_permission_response(exc.decision)
    except SourceSearchQueryTooLarge:
        return _vnext_public_error_response(status_code=400, detail="The topic is too long")
    return JSONResponse(status_code=200, content=jsonable_encoder(payload))
