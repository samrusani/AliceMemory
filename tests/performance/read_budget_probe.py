"""Measure native read paths on the same synthetic population."""
import json, os, sys, time, statistics, inspect
from pathlib import Path
repo, backend, location, user = sys.argv[1:5]
sys.path[:0] = [str(Path(repo)/"apps/api/src"), repo]
from contextlib import contextmanager
from uuid import UUID
from alicebot_api.db import user_connection
from alicebot_api.sqlite_store import sqlite_user_connection, SQLiteVNextStore
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_retrieval import VNextRetrievalService, VNextRetrievalRequest
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
@contextmanager
def store_context():
    if backend == "sqlite":
        with sqlite_user_connection(location, user) as conn:
            yield SQLiteVNextStore(conn, user)
    else:
        with user_connection(location, UUID(user)) as conn:
            yield PostgresVNextStore(conn)
identity = AgentIdentity(agent_id="budget", permission_profile="trusted_local_agent")
ceiling = ("public", "internal", "private", "unknown")
def pack():
    with store_context() as store:
        return VNextRetrievalService(store, embedding_provider=None, reranker_provider=None).compile_context_pack(
            VNextRetrievalRequest(query="synthetic budget observation", sensitivity_allowed=ceiling),
            source_fence=SourceReadFence.for_identity(identity))
def recall():
    return call_mcp_tool(MCPRuntimeContext(database_url="sqlite:///"+location if backend=="sqlite" else location, user_id=UUID(user)),
                         name="alice_recall", arguments={"query":"synthetic budget observation", "agent_id":"budget",
                                                          "permission_profile":"trusted_local_agent"})
actions = {"pack":pack, "recall":recall}
if backend == "postgres":
    from alicebot_api.routers.workspaces import _vnext_workspace_payload
    from alicebot_api.vnext_dogfooding import VNextDogfoodingService
    def workspace():
        with store_context() as store:
            kwargs = {"identity":identity} if "identity" in inspect.signature(_vnext_workspace_payload).parameters else {}
            return _vnext_workspace_payload(store, **kwargs)
    def dogfooding():
        with store_context() as store:
            service=VNextDogfoodingService(store)
            kwargs={"sensitivity_allowed":ceiling} if "sensitivity_allowed" in inspect.signature(service.dashboard).parameters else {}
            return service.dashboard(**kwargs)
    actions.update(workspace=workspace,dogfooding=dogfooding)
times={}
for name, action in actions.items():
    if name == "pack" and os.environ.get("ALICE_BUDGET_PROFILE_PATH"):
        import cProfile
        profile = cProfile.Profile()
        profile.runcall(action)
        profile.dump_stats(os.environ["ALICE_BUDGET_PROFILE_PATH"] + "." + Path(repo).name)
    else:
        action()
    samples=[]
    for _ in range(5):
        start=time.perf_counter(); action(); samples.append(time.perf_counter()-start)
    times[name]={"median":statistics.median(samples), "samples":samples}
print(json.dumps(times))
