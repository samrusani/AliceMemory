"""The pages say that the two routes answer and what they answer, and no page still says they fail.

The behaviour is tested in ``tests/integration/test_route_server_errors_postgres.py`` and
``tests/unit/test_vnext_route_server_errors.py``. The sentences are pinned by phrase.

Mutations, each one alone: delete the 409 body from the changelog entry; say again, in the changelog, the security
note or the tool reference, that ``POST /v0/vnext/queue/process-next`` answers HTTP 500 or that its claim statement is
ambiguous; delete the sentence that the 409 still tells a key with a ceiling that a project it may not read has the slug.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
PAGES = ("CHANGELOG.md", "docs/release/derived-labels-security-note-draft.md", "docs/alpha/mcp-tools.md")


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _entry(prefix: str) -> str:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    matches = [entry for entry in entries if entry.startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def test_the_changelog_says_what_a_project_slug_in_use_is_answered() -> None:
    entry = _entry(f"{MARK} `POST /v0/vnext/projects` answers HTTP 409")
    assert '{"detail": "vNext project slug is already in use"}' in entry
    assert "whether the slug was sent or made from the name" in entry
    assert "In v0.19.2, in v0.20.0 and on main until now it answered HTTP 500" in entry
    assert "to the owner, to an unbound `admin_agent` key and to an unbound `trusted_local_agent` key" in entry
    assert "The answer is the same for every caller and for every holder of the slug, readable or not" in entry
    assert "It names no project, and a refused create writes no project row and no event" in entry
    assert "is told that the slug is taken, in the words the owner is told, and nothing else about that project" in entry
    assert "A unique violation on any other constraint is not read as a slug conflict" in entry
    assert "still refused HTTP 403 before the route runs" in entry
    assert entry.endswith("No migration is required.")


def test_the_changelog_says_what_process_next_does_now_and_for_a_key_with_a_ceiling() -> None:
    entry = _entry(f"{MARK} `POST /v0/vnext/queue/process-next` works on PostgreSQL")
    assert "In v0.19.2, in v0.20.0 and on main until now its claim statement named `id` twice" in entry
    assert "the route answered HTTP 500 to every caller, whatever the queue held" in entry
    assert "The CTE now names the picked id `next_id`" in entry
    assert "An empty queue answers `idle`" in entry
    assert "`alicebot vnext queue process-next` runs the same claim" in entry
    assert "That key claims a task only when it may read the task's domain and sensitivity" in entry
    assert "judged by the fence that lists the tasks of `GET /v0/vnext/workspace`" in entry
    assert "A task above the ceiling is not claimed, locked or changed" in entry
    assert "the key is answered `idle`, as it is for an empty queue" in entry
    assert "a key locked to a project that reaches the handler claims nothing" in entry
    assert entry.endswith("No migration is required.")


def test_the_security_note_says_what_the_409_still_tells_a_key_with_a_ceiling() -> None:
    note = _text("docs/release/derived-labels-security-note-draft.md")
    paragraph = note.split(f"{MARK} two operator routes that answered HTTP 500 now answer.", 1)[1].split("\n", 1)[0]
    assert "The answer is the same for the owner, an admin key and a key with limits" in paragraph
    assert "so it carries no id, name, label or status of that project" in paragraph
    assert "It still tells a key with a ceiling that some project it may not read has the slug" in paragraph
    assert "A key can use that to test whether a hidden project has a slug it guesses, and learns nothing else about the project" in paragraph
    assert "It could do that before, because the route answered HTTP 500 for a slug in use and HTTP 201 for a free one" in paragraph
    assert "failed for every caller in v0.19.2 and v0.20.0, so no key claimed a task through it" in paragraph
    assert "claims only a task whose domain and sensitivity it may read" in paragraph
    assert "The claim skips a task above the ceiling without locking or changing it" in paragraph
    assert "Without that limit the fix would have let the trusted key run a confidential task" in paragraph


def test_the_tool_reference_names_both_answers() -> None:
    tools = _text("docs/alpha/mcp-tools.md")
    projects = tools.split(f"{MARK} `POST /v0/vnext/projects` answers HTTP 409", 1)[1].split("\n\n", 1)[0]
    assert '{"detail": "vNext project slug is already in use"}' in projects
    assert "The answer is the same for every caller and whoever holds the slug" in projects
    process = tools.split(f"{MARK} `POST /v0/vnext/queue/process-next` claims the oldest pending task", 1)[1].split("\n\n", 1)[0]
    assert '`{"status": "idle"}` when it has none' in process
    assert "claims only a task whose domain and sensitivity it may read" in process
    assert "Every other key is refused HTTP 403" in process


def test_no_page_still_says_process_next_fails() -> None:
    for page in PAGES:
        text = _text(page)
        assert "`POST /v0/vnext/queue/process-next` answers HTTP 500" not in text, page
        assert "claim statement is ambiguous" not in text, page
        assert "so it was probed and could not be read" not in text, page
        assert "so the sweep could not read what it returns" not in text, page
