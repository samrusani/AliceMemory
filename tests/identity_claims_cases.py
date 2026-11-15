"""Synthetic identity claim cases shared by both stores."""


def claim_cases():
    return (
        ("bound", "read", {"agent_id": "other-flat"}, "refused", 1),
        ("bound", "read", {"agent_identity": {"agent_id": "other-nested"}}, "refused", 1),
        ("bound", "read", {"agent_id": "other-mixed", "permission_profile": "admin_agent", "agent_identity": {}}, "refused", 1),
        ("bound", "read", {"agent_id": "other-own", "agent_identity": {"agent_id": "bound"}}, "refused", 1),
        ("bound", "read", {"permission_profile": "admin_agent", "agent_identity": {"agent_id": "bound"}}, "refused", 1),
        ("bound", "read", {"permission_profile": "unknown-alone"}, "refused", 1),
        ("bound", "read", {"agent_id": "bound", "permission_profile": "unknown-own"}, "refused", 1),
        ("bound", "read", {"agent_id": "other-invalid", "permission_profile": "unknown-other"}, "refused", 1),
        ("bound", "read", {"agent_id": "other-type", "agent_type": "unknown-type"}, "refused", 1),
        ("bound", "read", {"agent_identity": {"agent_id": "other-nested-invalid", "permission_profile": "unknown-nested"}}, "refused", 1),
        ("bound", "read", {"agent_identity": {"agent_id": "other-nested-type", "agent_type": "unknown-type"}}, "refused", 1),
        ("bound", "read", {"agent_id": "bound", "agent_type": "unknown-type", "project_scope": ["beta-invalid-type"]}, "refused", 1),
        ("bound", "read", {"agent_id": "bound", "permission_profile": "unknown-scope", "project_scope": ["beta-invalid-profile"]}, "refused", 1),
        ("bound", "read", {"agent_id": "bound", "agent_type": "unknown-type"}, "invalid", 0),
        ("bound", "read", {"agent_type": "unknown-type"}, "answered", 0),
        ("bound", "read", {"project_scope": ["beta-flat"]}, "refused", 1),
        ("bound", "read", {"project_scope": ["beta-mixed"], "agent_identity": {}}, "refused", 1),
        ("bound", "read", {"agent_id": "bound", "agent_identity": {"agent_id": "bound"}}, "answered", 0),
        ("bound", "read", {"agent_identity": {}}, "empty", 0),
        ("bound", "write", {"agent_id": "other-write", "permission_profile": "admin_agent", "agent_identity": {}}, "refused", 1),
        ("bound", "write", {"agent_id": "other-invalid-write", "permission_profile": "unknown-write"}, "refused", 1),
        ("bound", "write", {"project_scope": ["beta-write"], "agent_identity": {}}, "refused", 1),
        ("admin", "write", {"permission_profile": "read_only_agent"}, "rejected", 0),
        ("admin", "write", {"permission_profile": "read_only_agent", "agent_identity": {}}, "rejected", 0),
        ("admin", "write", {"agent_identity": {"permission_profile": "read_only_agent"}}, "partial", 0),
        ("admin", "write", {"agent_identity": {"permission_profile": "read_only_agent"}, "permission_profile": "admin_agent"}, "rejected", 0),
    )

