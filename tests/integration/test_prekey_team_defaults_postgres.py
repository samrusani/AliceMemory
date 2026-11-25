"""Keyless teams through the PostgreSQL tool and HTTP boundaries."""

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.keyless_team_contract import (
    PROJECT, TRUSTED, check_implicit_local_profile, check_lesson_standing,
    check_proposal_identity_contract, http_door, mcp_door, profile_claims,
)


@pytest.fixture(params=["mcp", "http"])
def call(label_harness, request):
    if request.param == "http":
        return http_door(label_harness)
    return mcp_door(MCPRuntimeContext(database_url=label_harness.urls["app"], user_id=label_harness.user_id))


@pytest.mark.parametrize("shape", ["flat", "agent_identity"])
@pytest.mark.parametrize("blank", ["omitted", None, "", "   "])
def test_postgres_default_uses_the_count_and_records_the_declared_writer(call, shape, blank):
    """Guard edit: use the original read-only fallback for an ordinary name."""
    check_implicit_local_profile(call, *profile_claims(shape, blank))


@pytest.mark.parametrize("claims", [TRUSTED, {**PROJECT, "project_scope": ["alpha"]}])
def test_postgres_keyless_writers_publish_and_retire(call, claims):
    """Guard edit: omit keyless publishing standing from the shared predicate."""
    check_lesson_standing(call, claims)


def test_http_proposal_keeps_its_identity_and_shape_requirements(label_harness):
    """Guard edit: apply the read-only fallback at the HTTP proposal route."""
    check_proposal_identity_contract(http_door(label_harness), missing="400", bad_lesson="422")
