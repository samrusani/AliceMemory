from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_reference_path_guide_links_major_integration_routes() -> None:
    guide = (REPO_ROOT / "docs" / "integrations" / "reference-paths.md").read_text(encoding="utf-8")

    assert "POST /v1/continuity/brief" in guide
    assert "scripts/run_hermes_bridge_demo.py" in guide
    assert "scripts/use_alice_with_openclaw.sh" in guide
    assert "alice_recall" in guide
    assert "alice_resume" in guide
    assert "provider registration and capability discovery" in guide
    assert "The adoption paths are Generic Agent, MCP Quickstart, Agent-Framework Function Tools, Hermes, and OpenClaw." in guide


def test_hermes_reference_doc_centers_provider_plus_mcp_and_one_call_continuity() -> None:
    hermes_doc = (REPO_ROOT / "docs" / "integrations" / "hermes.md").read_text(encoding="utf-8")

    assert "provider_plus_mcp" in hermes_doc
    assert "default three tools" in hermes_doc
    assert "alice_recall" in hermes_doc
    assert "alice_resume" in hermes_doc
    assert "ALICEBOT_AUTH_USER_ID" in hermes_doc
    assert "scripts/run_hermes_bridge_demo.py" in hermes_doc


def test_openclaw_reference_doc_covers_import_augmentation_and_one_call_reuse() -> None:
    openclaw_doc = (REPO_ROOT / "docs" / "integrations" / "openclaw.md").read_text(encoding="utf-8")

    assert "import plus augmentation" in openclaw_doc
    assert "POST /v1/continuity/brief" in openclaw_doc
    assert "alice_recall" in openclaw_doc
    assert "alice_resume" in openclaw_doc
    assert "scripts/use_alice_with_openclaw.sh" in openclaw_doc
    assert "generic_python_agent.py" in openclaw_doc
    assert "generic_typescript_agent.ts" in openclaw_doc


def test_reference_agent_examples_doc_points_to_both_runnable_examples_and_demo() -> None:
    examples_doc = (REPO_ROOT / "docs" / "examples" / "reference-agent-examples.md").read_text(encoding="utf-8")

    assert "generic_python_agent.py" in examples_doc
    assert "generic_typescript_agent.ts" in examples_doc
    assert "ALICE_USER_ID" in examples_doc
    assert "scripts/run_reference_agent_examples_demo.py" in examples_doc
    assert "--experimental-strip-types" in examples_doc
