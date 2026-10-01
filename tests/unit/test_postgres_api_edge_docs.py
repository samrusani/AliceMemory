"""The docs say what main changed at the Postgres API edge and keep v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as published. What main changed (DB-005,
DB-006, DB-009 of the internal security review of v0.19.0) is marked
``Unreleased (on main, not in v0.19.2):`` where a document describes the latest
release, and the changelog entry sits under the Unreleased heading with the
v0.19.2 behaviour stated beside each change.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "Unreleased (on main, not in v0.19.2):"
ENTRY_START = "The Postgres stack's HTTP API edge is hardened, from the internal security review of v0.19.0."


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _unreleased_changelog() -> str:
    changelog = _read("CHANGELOG.md")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]


def _entry() -> str:
    entries = [item for item in _unreleased_changelog().split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_entry_sits_under_unreleased_and_states_v0192_for_host_and_origin() -> None:
    """One Unreleased entry, with the v0.19.2 behaviour stated beside the Host and Origin rule.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete
    the sentence that says v0.19.2 checked the peer address only; delete the
    sentence that says keyed traffic is unchanged; delete the sentence that
    names the legacy ``/v0`` continuity routes as not covered; delete the
    ``ALICEBOT_ALLOWED_HOSTS`` instruction.
    """

    entry = _entry()
    assert "Host and Origin (DB-005):" in entry
    assert "is refused with the usual 401 `authentication_failed` unless its `Host` is `localhost`, `127.0.0.1` or `::1`" in entry
    assert "an exact name listed in the new `ALICEBOT_ALLOWED_HOSTS` setting (comma separated, no wildcard, port or scheme)" in entry
    assert "`X-Forwarded-Host` and `Forwarded` are not read" in entry
    assert "`null` is refused, and a `*` entry does not count" in entry
    assert "a request with an agent key is not checked, so keyed traffic and the Caddy topology are unchanged" in entry
    assert "In v0.19.2 both gates checked the peer address only" in entry
    assert "`POST /v1/workspaces/bootstrap` and `POST /v1/evals/runs`" in entry
    assert "A browser attack through that was not reproduced." in entry
    assert "must now list that name in `ALICEBOT_ALLOWED_HOSTS` or use an agent key" in entry
    assert "The legacy `/v0` continuity routes are not behind either gate and are not covered." in entry

    released = _read("CHANGELOG.md")[_read("CHANGELOG.md").index("## v0.19.2") :]
    assert ENTRY_START not in released
    assert "ALICEBOT_ALLOWED_HOSTS" not in released


def test_the_threat_model_names_dns_rebinding_and_the_host_rule_and_marks_main() -> None:
    """The deployment paragraph, the abuse-case row and the open-items bullet all say what main does, marked.

    Mutations, each one alone: delete the marker from any of the three; delete
    the DNS rebinding sentence; delete the line that says the browser leg was
    not reproduced; delete the line that names the legacy ``/v0`` routes as not
    covered; say v0.19.2 checks the Host.
    """

    model = _flat(_read("docs/security/threat-model.md"))
    paragraph = model[model.index(f"{MARKER} keyless operation also checks the name a request uses") :].split(" ### Assets")[0]
    paragraph = paragraph.split("| Memories,")[0]
    assert "DNS rebinding points a name the attacker owns at 127.0.0.1" in paragraph
    assert "`ALICEBOT_ALLOWED_HOSTS`" in paragraph
    assert "`X-Forwarded-Host` and `Forwarded` are never read" in paragraph
    assert "`null` is refused" in paragraph
    assert "In v0.19.2 both gates looked at the peer address only" in paragraph
    assert "The browser leg of the attack has not been reproduced in a real browser" in paragraph
    assert "are not behind these two gates and are not covered" in paragraph

    row = [line for line in _read("docs/security/threat-model.md").splitlines() if line.startswith("| DNS rebinding or a cross-origin request")]
    assert len(row) == 1
    assert MARKER in row[0]
    assert "Not reproduced in a real browser" in row[0]

    assert f"does not check the `Host` header of a keyless loopback request. Unreleased (on main, not in v0.19.2): the `Host` and `Origin` rules above are in (DB-005)." in model


def test_known_limitations_keeps_v0192_and_marks_main_for_host_and_origin() -> None:
    """The bullet still says v0.19.2 does not check Host, then says what main does, marked.

    Mutations: delete the v0.19.2 half; drop the marker; claim the check without
    the marker; drop the legacy ``/v0`` exclusion.
    """

    bullets = [
        _flat(line)
        for line in _read("docs/alpha/known-limitations.md").splitlines()
        if "does not check the `Host` header" in line
    ]
    assert len(bullets) == 1
    bullet = bullets[0]
    assert bullet.startswith("- the Postgres stack's HTTP API parses a JSON request body of any size before it authenticates")
    assert f"it does not check the `Host` header of a keyless loopback request. {MARKER} a keyless request is refused unless its `Host` is `localhost`, `127.0.0.1`, `::1` or a name listed in `ALICEBOT_ALLOWED_HOSTS`" in bullet
    assert "(DB-005)" in bullet
    assert bullet.endswith("The legacy `/v0` continuity routes are not behind that check")


def test_security_policy_and_deployment_guide_mark_the_host_rule_as_main() -> None:
    """SECURITY.md and the deployment guide say it, marked, and the example env documents the setting.

    Mutations: delete the marker from either document; delete the commented
    ``ALICEBOT_ALLOWED_HOSTS`` line from the example env.
    """

    security = _flat(_read("SECURITY.md"))
    assert f"{MARKER} a keyless request must also name this machine in `Host`" in security
    guide = _flat(_read("docs/deployment/single-tenant-self-hosted.md"))
    assert f"{MARKER} while keyless, the API also refuses a request whose `Host` is not `localhost`" in guide
    assert "this topology, which requires a key before Caddy starts, is unchanged" in guide
    assert "# ALICEBOT_ALLOWED_HOSTS=" in _read(".env.example")


def test_the_v0192_release_notes_still_list_the_findings_as_open() -> None:
    """Published release notes are immutable. Mutation: edit the DB-005 line in the v0.19.2 notes."""

    notes = _flat(_read("docs/release/v0.19.2-release-notes.md"))
    assert "**A keyless API does not check the Host header (DB-005).**" in notes
    assert "DB-005, DB-006, DB-008, DB-009, DB-010 and DB-011 are not fixed." in notes


def test_no_added_text_uses_an_em_dash_or_an_en_dash() -> None:
    """Mutation: write a dash into the changelog entry or any added paragraph."""

    for text in (
        _unreleased_changelog(),
        _read("SECURITY.md"),
        _read("docs/security/threat-model.md"),
        _read("docs/deployment/single-tenant-self-hosted.md"),
        _read(".env.example"),
        _read("apps/api/src/alicebot_api/keyless_edge.py"),
    ):
        assert "—" not in text
        assert "–" not in text
