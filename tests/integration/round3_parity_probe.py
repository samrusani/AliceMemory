"""Read the same synthetic rows using the selected revision's implementation."""
import inspect
import json
from pathlib import Path
import sys
from uuid import UUID

repo, location, user, profile, source_id = sys.argv[1:6]
sys.path[:0] = [str(Path(repo) / "apps/api/src"), repo]
from alicebot_api.db import user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.routers._vnext_shared import _vnext_load_source_trace
from alicebot_api.routers.workspaces import _vnext_workspace_payload

identity = None if profile == "owner" else AgentIdentity(agent_id="parity", permission_profile=profile)
with user_connection(location, UUID(user)) as conn:
    store = PostgresVNextStore(conn)
    options = {"identity": identity} if "identity" in inspect.signature(_vnext_workspace_payload).parameters else {}
    workspace = _vnext_workspace_payload(store, **options)
    options = {"identity": identity} if "identity" in inspect.signature(_vnext_load_source_trace).parameters else {}
    trace = _vnext_load_source_trace(store=store, source=store.get_source(source_id), **options)
    print(json.dumps({"trace": trace, "workspace": workspace}, default=str))
