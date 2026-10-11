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
    assert "HTTP 403 on every `/v0/vnext` route and a not_permitted error on every tool" in entry
    assert "Each surface answers with its standard refusal and none of them names the reason" in entry
    assert "are written to the server log at warning level" in entry
    assert "log the reason of a refusal at warning level, which a person running them reads on stderr" in entry
    assert "`reserved_project_name_in_identity_scope`" in entry
    # No claim of a stored event: a refused request rolls its transaction back (see the real-store tests).
    assert "key_escalation_rejected" not in entry and "reserved_project_scope_binding" not in entry
    assert "tells the owner" not in entry and "with a message that says so" not in entry
    assert "HTTP 400 when the payload names an agent and declares it" in entry
    assert "with per-project scoping on or off" in entry
    assert "`alicebot agent keys revoke <id>`" in entry
    assert "No migration is required" in entry
    assert "\u2014" not in entry and "\u2013" not in entry


def test_the_pages_say_the_reserved_name_cannot_be_a_binding() -> None:
    projects = _text("docs/alpha/projects.md")
    assert "It is also refused as a key's project binding and as the project scope an identity declares on the HTTP routes" in projects
    assert "is refused at every route and tool (HTTP 403, `not_permitted`, each with its standard text, which does not name the reason; the server log does)" in projects
    assert "Alice never writes it on a note, a source or an open loop" in projects
    assert "can store the literal name, where it is an ordinary free-form name that grants nothing, because no key can be bound to it" in projects
    assert "It is never stored." not in projects
    guide = _text("docs/alpha/agent-integration.md")
    assert "the name `~global` is reserved by Alice and\ncannot be a key's project scope" in guide
    assert "the reason is printed on stderr as a warning line\nbefore it" in guide
    assert "which does not name the reason. The reason, the id of the key" in guide
    assert "and the revoke command are written to the server log at warning level" in guide
    assert "key_escalation_rejected" not in guide.split("cannot be a key's project scope")[1].split("\n\n")[0]
    assert "with a message that says so" not in guide
