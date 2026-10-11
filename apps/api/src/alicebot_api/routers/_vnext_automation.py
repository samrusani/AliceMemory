from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from pydantic import Field

from alicebot_api.http_json_types import BoundedJsonObject
from alicebot_api.routers._vnext_shared import (
    VNextAgentRequest,
    _vnext_agent_actor,
    _vnext_int,
    _vnext_string_list,
)
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    PolicyDecision,
    agent_metadata,
)
from alicebot_api.vnext_projects import ProjectAutomationRequest


class VNextProjectAutomationRequest(VNextAgentRequest):
    user_id: UUID
    scope: BoundedJsonObject = Field(default_factory=dict)
    options: BoundedJsonObject = Field(default_factory=dict)

def _vnext_bool(mapping: dict[str, object], key: str, default: bool) -> bool:
    value = mapping.get(key)
    return value if isinstance(value, bool) else default

def _vnext_float(mapping: dict[str, object], key: str) -> float | None:
    value = mapping.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None

class _VNextModelGenerationOptions(TypedDict):
    generation_mode: str
    model_route_mode: str | None
    model_provider: str | None
    model: str | None
    model_temperature: float
    allow_cloud_private: bool

def _vnext_model_generation_options(options: dict[str, object]) -> _VNextModelGenerationOptions:
    generation_mode = options.get("generation_mode")
    route_mode = options.get("model_route_mode")
    provider = options.get("model_provider")
    model = options.get("model")
    temperature = _vnext_float(options, "model_temperature")
    if temperature is None or temperature < 0.0 or temperature > 2.0:
        temperature = 0.2
    return {
        "generation_mode": generation_mode if generation_mode in {"deterministic", "model_backed"} else "deterministic",
        "model_route_mode": route_mode
        if route_mode in {"local_only", "cloud_allowed", "cloud_requires_approval", "model_disabled"}
        else None,
        "model_provider": provider if isinstance(provider, str) else None,
        "model": model if isinstance(model, str) else None,
        "model_temperature": temperature,
        "allow_cloud_private": _vnext_bool(options, "allow_cloud_private", False),
    }

def _vnext_project_automation_request(
    request: VNextProjectAutomationRequest,
    *,
    identity: AgentIdentity | None = None,
    decision: PolicyDecision | None = None,
) -> ProjectAutomationRequest:
    options = request.options
    scope = request.scope
    explicit_project_id = options.get("project_id") or scope.get("project_id")
    canonical_projects = (
        decision.effective_project_scope
        if decision is not None
        else tuple(request.project_scope) or _vnext_string_list(scope, "projects")
    )
    if isinstance(explicit_project_id, str) and explicit_project_id.strip():
        project_id = explicit_project_id.strip()
        if canonical_projects and project_id not in canonical_projects:
            raise ValueError("project_id must be contained in the canonical project_scope")
    elif len(canonical_projects) == 1:
        project_id = canonical_projects[0]
    elif len(canonical_projects) > 1:
        raise ValueError("project automation requires one project_id when project_scope contains multiple projects")
    else:
        project_id = None
    person_id = options.get("person_id") or scope.get("person_id")
    actor_type, actor_id = _vnext_agent_actor(identity, fallback="system")
    return ProjectAutomationRequest(
        domains=decision.effective_domains if decision is not None else _vnext_string_list(scope, "domains"),
        sensitivity_allowed=decision.effective_sensitivity_allowed
        if decision is not None
        else _vnext_string_list(options, "sensitivity_allowed") or ("public", "internal", "private", "unknown"),
        project_id=project_id,
        person_id=str(person_id) if isinstance(person_id, str) else None,
        max_items=_vnext_int(options, "max_items", 8),
        generated_by=actor_type,
        actor_id=actor_id,
        trace_id=request.trace_id,
        run_id=identity.agent_run_id if identity is not None else None,
        agent_identity=identity.to_record() if identity is not None else None,
        policy_decision=decision.to_record() if decision is not None else None,
        metadata_json=agent_metadata(identity, decision),
        **_vnext_model_generation_options(options),
    )
