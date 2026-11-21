"""Classification and smallest HTTP requests for public sensitivity inputs."""

from dataclasses import dataclass, field


CREATE = "create with ceiling"
FAN_OUT = "label one call's new rows with ceiling"
CHANGE = "change a stored label with owner or admin"
OWNER = "owner command"
READ = "read filter"
LEVELS = ("confidential", "highly_sensitive", "sacred", "regulated")
WITHIN = ("private", "internal", "public", "unknown", None)


@dataclass(frozen=True)
class HttpIntake:
    kind: str
    payload: dict[str, object] = field(default_factory=dict)
    request_path: str | None = None
    success: int = 201
    unavailable: str = ""


SHADOWED = "The generic sync route matches the path first and answers 422."
HTTP = {
    ("POST", "/v0/vnext/agents/ingest-output", "sensitivity"): HttpIntake(
        FAN_OUT, {"title": "Synthetic output", "content": "A synthetic status note."},
    ),
    ("POST", "/v0/vnext/connectors/browser-clipper/capture", "sensitivity"): HttpIntake(
        CREATE, {"url": "https://example.invalid/note", "page_text": "A synthetic clip."},
    ),
    ("POST", "/v0/vnext/connectors/local-folder/sync", "default_sensitivity"): HttpIntake(
        FAN_OUT, {"paths": ["synthetic-note"]}, unavailable=SHADOWED,
    ),
    ("POST", "/v0/vnext/connectors/telegram/sync", "default_sensitivity"): HttpIntake(
        FAN_OUT, {"updates": [{"update_id": 1}], "allowed_chat_ids": ["1"]}, unavailable=SHADOWED,
    ),
    ("PATCH", "/v0/vnext/connectors/{connector_name}/config", "default_sensitivity"): HttpIntake(CHANGE),
    ("POST", "/v0/vnext/connectors/{connector_name}/sync", "default_sensitivity"): HttpIntake(
        FAN_OUT, {"items": [{"url": "https://example.invalid/sync", "text": "A synthetic sync note."}]},
        request_path="/v0/vnext/connectors/browser_clipper/sync",
    ),
    ("POST", "/v0/vnext/memories/commit", "sensitivity"): HttpIntake(
        CREATE, {"title": "Synthetic commit", "canonical_text": "A synthetic commit note."},
    ),
    ("POST", "/v0/vnext/memories/{memory_id}/review", "sensitivity"): HttpIntake(CHANGE),
    ("POST", "/v0/vnext/memory-proposals", "sensitivity"): HttpIntake(
        CREATE, {"title": "Synthetic proposal", "canonical_text": "A synthetic proposal note."},
    ),
    ("POST", "/v0/vnext/open-loops", "sensitivity"): HttpIntake(CREATE, {"title": "Synthetic open loop"}),
    ("POST", "/v0/vnext/projects", "sensitivity"): HttpIntake(CREATE, {"name": "Synthetic project"}),
    ("POST", "/v0/vnext/queue/tasks", "sensitivity"): HttpIntake(
        CREATE, {"title": "Synthetic task", "task_type": "summarize", "instructions": "Summarize synthetic notes."},
    ),
    ("PUT", "/v0/vnext/settings/brain-charter", "sensitivity"): HttpIntake(
        CREATE, {"content_markdown": "A synthetic charter."}, success=200,
    ),
    ("POST", "/v0/vnext/sources", "sensitivity"): HttpIntake(CREATE, {"raw_text": "A synthetic source note."}),
    ("POST", "/v0/vnext/sources/{source_id}/review", "sensitivity"): HttpIntake(CHANGE),
}

MCP = {
    ("alice_capture", "sensitivity"): CREATE,
    ("alice_memory_commit", "sensitivity"): CREATE,
    ("alice_vnext_capture", "sensitivity"): CREATE,
    ("alice_vnext_ingest_agent_output", "sensitivity"): FAN_OUT,
    ("alice_vnext_queue_task", "sensitivity"): CREATE,
    ("alice_vnext_open_loops", "sensitivity"): READ,
    ("alice_vnext_propose_memory", "sensitivity"): CREATE,
    ("alice_vnext_commit_memory", "sensitivity"): CREATE,
}

CLI = {
    "alicebot vnext connectors configure": OWNER,
    "alicebot vnext connectors ingest": OWNER,
    "alicebot vnext connectors local-folder add-path": OWNER,
    "alicebot vnext connectors local-folder remove-path": OWNER,
    "alicebot vnext connectors local-folder sync": OWNER,
    "alicebot vnext connectors local-folder watch": OWNER,
    "alicebot vnext connectors browser-clipper capture": OWNER,
    "alicebot vnext sources capture-text": OWNER,
    "alicebot vnext sources capture-file": OWNER,
    "alicebot vnext sources import-markdown": OWNER,
    "alicebot vnext sources import-chatgpt": OWNER,
    "alicebot vnext queue add": OWNER,
    "alicebot vnext memories commit": CREATE,
    "alicebot vnext agents propose-memory": CREATE,
    "alicebot vnext agents ingest-output": FAN_OUT,
    "alice-memory import-markdown": OWNER,
    "alice-memory import-chatgpt": OWNER,
}
