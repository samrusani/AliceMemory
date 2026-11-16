"""A non-capture CLI policy refusal is reported as a filesystem failure."""

import json
import os
import subprocess
import sys

from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def test_cli_read_only_forget_refusal_uses_filesystem_error(label_harness):
    """The limitations page changes if the CLI reports the refusal differently.

    Mutation: use ``_CLI_COMMAND_FAILED`` in the OSError branch of the CLI runner.
    """
    h = label_harness
    row = h.memory(scope=["alpha"])
    with h.store() as store:
        _, key = create_agent_key(
            store, user_id=h.user_id, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha",
        )
    args = [
        sys.executable, "-c", "import sys; from alicebot_api.cli import main; sys.exit(main())",
        "--database-url", h.urls["app"], "--user-id", str(h.user_id), "vnext", "memories", "forget",
        str(row["id"]), "--agent-id", "reader", "--permission-profile", "read_only_agent", "--project-scope", "alpha",
    ]
    result = subprocess.run(args, env={**os.environ, "ALICE_AGENT_API_KEY": key}, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "filesystem_operation_failed"
    with h.store() as store:
        assert store.get_memory(str(row["id"]))["status"] == "active"
