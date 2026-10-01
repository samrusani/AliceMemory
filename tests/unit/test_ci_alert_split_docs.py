"""The docs say how main splits the alert job from the job that installs packages (DB-008).

v0.19.2 is released, so its notes keep saying the canary and archive maintenance hold
issue-write authority in the job that installs. What main changed is marked
``Unreleased (on main, not in v0.19.2):`` where a document describes the latest
release, and sits under the changelog's Unreleased heading.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNRELEASED = "Unreleased (on main, not in v0.19.2):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _unreleased_changelog() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]


def _changelog_entry_containing(text: str) -> str:
    entries = [item for item in _unreleased_changelog().split("\n- ")[1:] if text in _flat(item)]
    assert len(entries) == 1, text
    return _flat(entries[0])


def test_the_changelog_states_the_canary_split_and_what_v0192_did() -> None:
    """One Unreleased entry carries the canary sentences, with the v0.19.2 behaviour stated.

    Mutations, each one alone: move the sentences under the v0.19.2 heading; delete
    the sentence that says what v0.19.2 did; delete the sentence that names the
    separate job and its limits; delete the sentence that says nothing else changed.
    """

    entry = _changelog_entry_containing("Separately, the weekly real-host canary no longer holds issue-write permission")
    assert (
        "In v0.19.2 one job installed the current host CLIs with `npm install ...@latest` and `pip install "
        "hermes-agent` and held `issues: write`, and its checkout kept the job token available to later steps, so a "
        "compromised package that ran in that job could use the token to open, edit and comment on issues."
    ) in entry
    assert (
        "The canary job now holds `contents: read` only and its checkout keeps no credentials, and a separate job, "
        "`canary-alert`, which holds `issues: write` and checks out nothing, runs no shell and installs nothing, opens "
        "the `[ops]` alert issue when the canary job fails."
    ) in entry
    assert "What the canary runs, and when it alerts, are unchanged." in entry


def test_the_threat_model_and_dependency_posture_mark_the_canary_split_unreleased() -> None:
    """The threat model keeps the v0.19.2 open item and adds the fix marked as main only.

    The dependency posture no longer says every job selects its tool versions.
    Mutations, each one alone: delete the DB-008 sentences from the threat model;
    drop the Unreleased marker from the dependency posture; put back the sentence
    that says tool versions installed inside jobs are explicitly selected without
    the word most; delete the sentence that names the canary as unpinned on purpose.
    """

    threat_model = _read("docs/security/threat-model.md")
    posture = _read("docs/security/dependency-posture.md")
    assert "real-host canary and archive maintenance, hold issue-write authority" in threat_model
    assert (
        "DB-008, the weekly real-host canary holding `issues: write` in the job that installs the current host CLIs."
    ) in threat_model
    assert "starts only when the canary job's result is `failure`" in threat_model
    assert "v0.19.2 installed unpinned packages in a job that could open and comment on issues" in threat_model
    assert "Tool versions installed inside most jobs, such as Gitleaks, are explicitly selected" in posture
    assert "Two scheduled jobs do not pin on purpose." in posture
    assert "The weekly real-host canary installs the current Claude Code, Hermes, OpenCode and Codex CLIs" in posture
    assert f"{UNRELEASED} the canary job holds `contents: read` only" in posture
