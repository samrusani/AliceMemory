"""Dedicated connector request bodies currently meet the generic sync model."""

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


@pytest.mark.parametrize(("connector", "body"), [
    ("telegram", {"updates": [{"update_id": 1}], "allowed_chat_ids": ["1"]}),
    ("local-folder", {"paths": ["/synthetic/path"]}),
])
def test_dedicated_connector_body_hits_generic_sync_model(label_harness, connector, body):
    """The limitations page changes when the dedicated route becomes reachable.

    Mutation: rename the generic sync route so it no longer shadows the dedicated routes.
    """
    key = label_harness.key("admin_agent")
    status, response, _ = label_harness.request(
        "POST", f"/v0/vnext/connectors/{connector}/sync", payload=body, key=key,
    )
    assert status == 422
    assert all(item["type"] == "extra_forbidden" for item in response["detail"])
    assert {item["loc"][-1] for item in response["detail"]} == set(body)
