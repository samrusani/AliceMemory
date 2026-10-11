"""The pages and the changelog say that the reserved project name cannot be a key binding, and which versions had the old behaviour."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _entry(prefix: str) -> str:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    matches = [entry for entry in entries if entry.startswith(f"{MARK} {prefix}")]
    assert len(matches) == 1, prefix
    return matches[0]


def test_the_changelog_has_one_entry_for_the_reserved_project_name_in_a_key_binding() -> None:
    """Mutation: delete a sentence that names a version, a door or the way a stored key is refused."""

    entry = _entry("a key cannot be bound to the project name `~global`")
    assert "`alicebot agent keys create --project-scope '~global'` stored such a key" in entry
    assert "In v0.20.0 the name was an ordinary one" in entry and "no release had it" in entry
    assert "no HTTP route and no MCP tool creates one" in entry
    assert "HTTP 403 on every `/v0/vnext` route, `not_permitted` on every tool" in entry
    assert "`reserved_project_scope_binding`" in entry and "`reserved_project_name_in_identity_scope`" in entry
    assert "HTTP 400 when the payload names an agent and declares it" in entry
    assert "with per-project scoping on or off" in entry
    assert "`alicebot agent keys revoke`" in entry
    assert "No migration is required" in entry
    assert "\u2014" not in entry and "\u2013" not in entry


def test_the_pages_say_the_reserved_name_cannot_be_a_binding() -> None:
    projects = _text("docs/alpha/projects.md")
    assert "It is also refused as a key's project binding and as the project scope an identity declares on the HTTP routes" in projects
    assert "is refused at every route and tool (HTTP 403, `not_permitted`)" in projects
    guide = _text("docs/alpha/agent-integration.md")
    assert "the name `~global` is reserved by Alice and\ncannot be a key's project scope" in guide
    assert "recorded as an event with the reason\n`reserved_project_scope_binding`" in guide
