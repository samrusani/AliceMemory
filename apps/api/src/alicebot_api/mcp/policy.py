"""MCP agent-identity resolution and policy enforcement helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from alicebot_api.json_bounds import is_remembered, remember_durable, replay_durable
from alicebot_api.project_view import (
    ProjectView,
    effective_scope_for_view,
    resolve_view_at_edge,
    working_folder,
)
from alicebot_api.vnext_project_scope import holds_global_marker
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentIdentityValidationError,
    PolicyDecision,
    append_policy_events,
    evaluate_agent_policy,
)
from alicebot_api.vnext_agent_keys import AgentKeyAuthenticationError, resolve_agent_identity
from alicebot_api.vnext_promotion_policy import PromotionCandidate, PromotionSettings
from alicebot_api.vnext_store import PostgresVNextStore

from .runtime import _is_sqlite_backend, _sqlite_path_from_url, _vnext_store_context
from .types import (
    MCPArgumentError,
    MCPInvalidRequestError,
    MCPNotPermittedError,
    MCPRuntimeContext,
    MCPToolError,
)


AGENT_API_KEY_ENV = "ALICE_AGENT_API_KEY"


def _agent_identity_from_arguments(context: MCPRuntimeContext, arguments: Mapping[str, object]) -> AgentIdentity | None:
    """Resolve the calling agent's identity for one MCP tool call.

    With ``ALICE_AGENT_API_KEY`` the identity is resolved and enforced against
    the issued key record exactly like the HTTP surface; a bad or revoked key
    answers as it always did. Without the key the call is keyless: it is
    refused with the not-permitted answer once the vault holds at least one
    active key (reads included, the same rule the HTTP surface applies), and
    while the vault holds no active key it stays local operator tooling that
    honors the payload identity with the default ``unauthenticated_local``
    auth marker. The rule lifts by itself when the last active key is gone
    (revoked keys do not count), so revoking every key returns the vault to
    the keyless behaviour.
    """

    if context.agent_identity_resolved:
        return context.agent_identity

    raw_key = (os.environ.get(AGENT_API_KEY_ENV) or "").strip() or None
    if raw_key is None:
        with _vnext_store_context(context) as store:
            if store.count_active_agent_api_keys() > 0:
                raise MCPNotPermittedError(
                    "agent API keys are configured for this vault; every call needs "
                    f"{AGENT_API_KEY_ENV} (mint a key with 'alice-memory keys create')"
                )
        try:
            return AgentIdentity.from_payload(arguments)
        except AgentIdentityValidationError as exc:
            raise MCPArgumentError(str(exc)) from exc
    try:
        with _vnext_store_context(context) as store:
            return resolve_agent_identity(
                store,
                user_id=context.user_id,
                raw_key=raw_key,
                payload=arguments,
            )
    except AgentKeyAuthenticationError as exc:
        # An invalid or revoked key, a key of another user, or a claim of
        # another agent, a higher profile or a wider project scope than the key
        # grants. The agent cannot fix any of these by retrying.
        raise MCPNotPermittedError(str(exc)) from exc
    except AgentIdentityValidationError as exc:
        raise MCPArgumentError(str(exc)) from exc


RESERVED_PROJECT_NAME_MESSAGE = (
    "the project name ~global is reserved by Alice and cannot be sent as a project, "
    "a project scope or an identity scope"
)
#: The argument names that carry project names, on any tool.
_PROJECT_NAME_ARGUMENTS = ("projects", "project", "project_scope")


def _refuse_reserved_project_marker(arguments: Mapping[str, object]) -> None:
    """Refuse the reserved global marker as caller input, on every tool (spec 6.1).

    ``~global`` exists only inside a request tuple that Alice builds from a project
    view. A caller who sent it in ``projects``, ``project`` or ``project_scope``, or
    declared it in ``agent_identity.project_scope``, would get the global rows of
    the vault through a name that no policy check ever saw as a scope, so the
    request is refused before any handler runs. The refusal does not depend on the
    per-project switch, because the stores read the marker whenever a request holds
    it. The message is fixed words and never echoes the request.
    """

    sources: list[object] = [arguments.get(name) for name in _PROJECT_NAME_ARGUMENTS]
    nested = arguments.get("agent_identity")
    if isinstance(nested, Mapping):
        sources.append(nested.get("project_scope"))
    for value in sources:
        if value is not None and holds_global_marker(value):
            raise MCPInvalidRequestError(RESERVED_PROJECT_NAME_MESSAGE)


def _default_project_view(context: MCPRuntimeContext) -> ProjectView:
    """The view of a call that named no project (spec 6.1, 6.5, 6.6).

    Read from the switch and the start folder on each call: ``--project-dir``
    (``context.project_dir``), ``ALICE_PROJECT_DIR``, then the working folder the
    host started the server in. Scoping off, which is the release default until the
    flip, gives an unscoped view and the call reads what it always read. A backend
    other than SQLite has no project view (spec 6.1) and gets the unscoped one.
    """

    if not _is_sqlite_backend(context):
        return ProjectView.unscoped()
    return resolve_view_at_edge(
        db_path=Path(_sqlite_path_from_url(context.database_url)),
        environ=os.environ,
        argument_dir=context.project_dir,
        hook_cwd=None,
        process_cwd=working_folder(),
    ).view


def _policy_checked(
    store: PostgresVNextStore,
    *,
    identity: AgentIdentity | None,
    action: str,
    # Required, with no default. A handler that forgets the view fails the type
    # check and the classification test, not a reader. ``ProjectView.unscoped()``
    # written at the call site says the call has no project view on purpose, and
    # every such site is on the allowlist in ``test_per_project_policy_views``.
    project_view: ProjectView,
    domains: tuple[str, ...] = (),
    sensitivity_allowed: tuple[str, ...] = ("public", "internal", "private", "unknown"),
    project_scope: tuple[str, ...] = (),
    project_floor: tuple[str, ...] = (),
    workflow_type: str | None = None,
    write_policy: str | None = None,
    require_explicit_project_scope: bool = False,
    require_unfiltered_target: bool = False,
    target_type: str | None = None,
    target_id: str | None = None,
    promotion_settings: PromotionSettings | None = None,
    promotion_candidate: PromotionCandidate | None = None,
    owner_verified: bool = False,
) -> tuple[str, str | None, PolicyDecision]:
    # A tool call that is run again after a reader overflowed (``retry_cutting_deep_json``) must not write its audit
    # events twice. When the first attempt's transaction committed this check, its result comes back and the identity row
    # and the policy events, which are in the file already, are not written again. A first attempt that rolled back left
    # nothing, and the check runs and writes as it always does.
    check = repr((
        identity.agent_id if identity is not None else None, action, domains, sensitivity_allowed, project_scope, project_floor,
        workflow_type, write_policy, require_explicit_project_scope, require_unfiltered_target, target_type, target_id,
        owner_verified,
    ))
    done = replay_durable(check)
    if is_remembered(done):
        return done  # type: ignore[no-any-return]
    if identity is not None:
        store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {"last_agent_run_id": identity.agent_run_id, "last_task_id": identity.task_id},
            },
            actor_type="agent",
        )
    decision = evaluate_agent_policy(
        identity=identity,
        action=action,
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        project_scope=project_scope,
        project_floor=project_floor,
        workflow_type=workflow_type,
        write_policy=write_policy,
        require_explicit_project_scope=require_explicit_project_scope,
        promotion_settings=promotion_settings,
        promotion_candidate=promotion_candidate,
        owner_verified=owner_verified,
    )
    # The policy engine is not told about the view: a detected project is not a
    # grant and does not satisfy ``project_scope_required``. The view only fills a
    # scope the engine left empty, for a caller who named no project, an identity
    # that declares none and a key that is not locked to one (spec 6.1).
    view_scope = effective_scope_for_view(
        view=project_view,
        decision_scope=decision.effective_project_scope,
        requested_scope=project_scope,
        identity_scope=identity.project_scope if identity is not None else (),
        identity_locked=identity.project_scope_locked if identity is not None else False,
    )
    if view_scope != decision.effective_project_scope:
        decision = replace(decision, effective_project_scope=view_scope)
    if require_unfiltered_target and decision.decision == "allowed_with_filtering":
        decision = replace(
            decision,
            decision="blocked",
            reasons=tuple(dict.fromkeys((*decision.reasons, "artifact_target_filtering_not_permitted"))),
        )
    append_policy_events(
        store,
        identity=identity,
        decision=decision,
        target_type=target_type,
        target_id=target_id,
    )
    checked = ("agent", identity.agent_id, decision) if identity is not None else ("system", None, decision)
    remember_durable(check, checked)
    return checked


def _raise_mcp_policy_blocked(decision: PolicyDecision) -> None:
    # The one place a blocked policy decision becomes an MCP error. The reasons
    # stay in this text for the log and the policy events; the server answers
    # not_permitted with fixed words and never sends them.
    raise MCPNotPermittedError(f"agent policy blocked: {', '.join(decision.reasons) or decision.action}")


def _mcp_agent_policy_preflight(
    context: MCPRuntimeContext,
    arguments: Mapping[str, object],
    *,
    action: str,
    project_view: ProjectView,
    domains: tuple[str, ...] = (),
    sensitivity_allowed: tuple[str, ...] = ("public", "internal", "private", "unknown"),
    project_scope: tuple[str, ...] = (),
    workflow_type: str | None = None,
    write_policy: str | None = None,
) -> PolicyDecision:
    identity = _agent_identity_from_arguments(context, arguments)
    blocked_decision: PolicyDecision | None = None
    decision: PolicyDecision | None = None
    with _vnext_store_context(context) as store:
        _actor_type, _actor_id, decision = _policy_checked(
            store,
            identity=identity,
            action=action,
            project_view=project_view,
            domains=domains,
            sensitivity_allowed=sensitivity_allowed,
            project_scope=project_scope,
            workflow_type=workflow_type,
            write_policy=write_policy,
        )
        if decision.decision == "blocked":
            blocked_decision = decision
    if blocked_decision is not None:
        _raise_mcp_policy_blocked(blocked_decision)
    if decision is None:
        raise MCPToolError("agent policy preflight did not complete")
    return decision
