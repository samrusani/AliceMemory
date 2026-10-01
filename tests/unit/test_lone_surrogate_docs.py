"""The docs say what main does with a lone surrogate and keep v0.19.2's behaviour as the comparison.

v0.19.2 is released, so its notes and the sentences that describe it stay as
they are. What main changed is marked ``Unreleased (on main, not in v0.19.2):``
where a document describes the latest release, and sits under the changelog's
Unreleased heading.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _unreleased_changelog() -> str:
    changelog = _read("CHANGELOG.md")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]


def test_the_changelog_entry_sits_under_unreleased_and_states_v0192() -> None:
    """One Unreleased entry, with both halves and the v0.19.2 behaviour beside each.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete
    the sentence that says v0.19.2 answered HTTP 500; delete the 404 sentence;
    delete the sentence about plugin 0.5.2; delete the ``--force`` install note.
    """

    entries = [item for item in _unreleased_changelog().split("\n- ")[1:]]
    assert len(entries) == 1
    entry = _flat(entries[0])
    assert 'A request body that holds a lone surrogate, for example the JSON escape `"\\ud800"`, is refused with HTTP 422 on every route.' in entry
    assert "a string field, a value inside a dict or list that a route takes as any value, and an object key" in entry
    assert "so the answer was HTTP 500, which is what dropped a Hermes turn" in entry
    assert "In v0.19.2 pydantic refused a surrogate in a string field" in entry
    assert "A surrogate in a dict, a list or a key inside one was not checked and the request reached the route." in entry
    assert "answers 422 now and answered 404 in v0.19.2" in entry
    assert "Hermes provider 0.5.3 replaces each lone surrogate with U+FFFD" in entry
    assert "The plugin logs and counts nothing about a replacement." in entry
    assert "In plugin 0.5.2, which is in v0.19.2, the surrogate was sent" in entry
    assert "scripts/install_hermes_alice_memory_provider.py --force" in entry
    assert "A symlink install picks up the change." in entry

    released = _read("CHANGELOG.md")[_read("CHANGELOG.md").index("## v0.19.2") :]
    assert "Hermes provider 0.5.3" not in released
    assert "is refused with HTTP 422 on every route" not in released


def test_no_added_line_uses_an_em_dash_or_an_en_dash() -> None:
    """Mutation: write a dash into the Unreleased entry or into either guide paragraph."""

    guide = _flat(_read("docs/integrations/hermes-memory-provider.md"))
    paragraph = guide[guide.index("Unreleased (on main, not in v0.19.2): plugin 0.5.3") :].split(" From v0.19.2,")[0]
    for text in (_unreleased_changelog(), paragraph, _read("docs/integrations/hermes-bridge-operator-guide.md")[:700]):
        assert "\u2014" not in text
        assert "\u2013" not in text


def test_the_known_limitation_keeps_v0192_and_marks_main() -> None:
    """The bullet still says what v0.19.2 does and adds what main does, marked.

    Mutations: delete the v0.19.2 half of the bullet; drop the marker; claim the
    turn is saved without saying it is main.
    """

    bullets = [
        _flat(line)
        for line in _read("docs/alpha/known-limitations.md").splitlines()
        if "lone surrogate" in line
    ]
    assert len(bullets) == 1
    bullet = bullets[0]
    assert bullet.startswith(
        "- the server answers a `POST /v0/continuity/captures/candidates` body that carries a lone surrogate with HTTP 500, "
        "so a Hermes turn that carries one is not saved."
    )
    assert bullet.endswith(
        "Unreleased (on main, not in v0.19.2): every route answers a body that carries one with HTTP 422, "
        "and Hermes provider 0.5.3 replaces it with U+FFFD, so the turn is saved"
    )


def test_the_provider_guide_marks_plugin_053_as_main_and_keeps_052_as_v0192() -> None:
    """The v0.19.2 paragraph stays and the 0.5.3 paragraph is marked, ahead of the ``/v1`` paragraph.

    Mutations: delete the marker; delete the ``--force`` note; remove the v0.19.2
    paragraph about 0.5.2; say 0.5.3 is in v0.19.2.
    """

    guide = _flat(_read("docs/integrations/hermes-memory-provider.md"))
    released = "From v0.19.2, plugin 0.5.2 sends the user text and the assistant text"
    marked = "Unreleased (on main, not in v0.19.2): plugin 0.5.3 replaces each lone surrogate"
    later = "From v0.19.2, `POST /v1/memory/operations/commit` reads the role."
    assert guide.count(released) == guide.count(marked) == guide.count(later) == 1
    assert guide.index(released) < guide.index(marked) < guide.index(later)
    paragraph = guide[guide.index(marked) : guide.index(later)]
    assert "Plugin 0.5.2 sent the surrogate, and the server answered HTTP 500 and dropped the turn." in paragraph
    assert "On main every route answers a request body that carries a lone surrogate with HTTP 422." in paragraph
    assert "`./scripts/install_hermes_alice_memory_provider.py --force`" in paragraph
    assert "in v0.19.2" not in paragraph.replace("not in v0.19.2", "")


def test_the_http_error_docs_mark_the_new_422_and_keep_v0192() -> None:
    """The agent integration guide says what main does and what v0.19.2 does.

    Mutations: delete the marker; delete the v0.19.2 sentence; say main's
    behaviour without the marker.
    """

    doc = _flat(_read("docs/alpha/agent-integration.md"))
    marked = "Unreleased (on main, not in v0.19.2): a JSON request body that holds a lone surrogate"
    assert doc.count(marked) == 1
    paragraph = doc[doc.index(marked) :].split(" ## Scopes")[0]
    assert "is refused with HTTP 422 and the array `detail` of a validation error." in paragraph
    assert "The error says where the text is and does not repeat it." in paragraph
    assert "v0.19.2 answers HTTP 500 for a surrogate in a string field." in paragraph
