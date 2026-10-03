"""The docs say how main splits the alert job from the job that installs packages (DB-008).

v0.19.2 is released, so its notes keep saying the canary and archive maintenance hold
issue-write authority in the job that installs. What v0.20.0 changed is marked
``From v0.20.0,`` where a document describes the latest
release, and sits under the changelog's v0.20.0 heading.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROM_V0200 = "From v0.20.0,"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _v0200_changelog() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return changelog[changelog.index("## v0.20.0 \u2014 2026-10-02") + len("## v0.20.0 \u2014 2026-10-02") : changelog.index("## v0.19.2")]


def _changelog_entry_containing(text: str) -> str:
    entries = [item for item in _v0200_changelog().split("\n- ")[1:] if text in _flat(item)]
    assert len(entries) == 1, text
    return _flat(entries[0])


def test_the_changelog_states_the_canary_and_archive_split_and_what_v0192_did() -> None:
    """One v0.20.0 entry carries both splits, with the v0.19.2 behaviour stated.

    Mutations, each one alone: move the sentences under the v0.19.2 heading; delete
    the sentence that says what v0.19.2 did to the canary; delete the sentence that
    names the separate canary job and its limits; delete the archive maintenance
    sentences; say the archive alert reads the schedule from a job output; delete
    the sentence that says nothing else changed.
    """

    entry = _changelog_entry_containing(
        "Separately, the weekly real-host canary and the nightly archive maintenance no longer hold issue-write"
    )
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
    assert (
        "Archive maintenance had the same arrangement: in v0.19.2 `issues: write` was set for the whole workflow, and "
        "its job ran `pip install --upgrade pip` and installed the dev extras by version range."
    ) in entry
    assert (
        "Its job now holds `contents: read` only and its checkout keeps no credentials, and a separate job, "
        "`archive-alert`, opens the `[ops] archive maintenance failure` issue when the maintenance job fails."
    ) in entry
    assert (
        "That job reads the schedule it names from the event that started the run, not from a value the installing "
        "job wrote."
    ) in entry
    assert "What the canary and archive maintenance run, and when they alert, are unchanged." in entry


def test_the_threat_model_and_dependency_posture_mark_the_split_from_v0200() -> None:
    """The threat model keeps the v0.19.2 open item and adds the fix marked from v0.20.0.

    The dependency posture no longer says every job selects its tool versions.
    Mutations, each one alone: delete the DB-008 sentences from the threat model;
    delete the archive sentence from the threat model; drop the ``From v0.20.0,`` marker
    from the dependency posture; put back the sentence that says tool versions
    installed inside jobs are explicitly selected without the word most; delete the
    sentence that names the canary as unpinned on purpose.
    """

    threat_model = _read("docs/security/threat-model.md")
    posture = _read("docs/security/dependency-posture.md")
    assert "real-host canary and archive maintenance, hold issue-write authority" in threat_model
    assert (
        "DB-008, the weekly real-host canary holding `issues: write` in the job that installs the current host CLIs."
    ) in threat_model
    assert "starts only when the canary job's result is `failure`" in threat_model
    assert "v0.19.2 installed unpinned packages in a job that could open and comment on issues" in threat_model
    assert (
        "Archive maintenance, which had the same arrangement, is split the same way: its job holds `contents: read` "
        "only, and a separate `archive-alert` job holds `issues: write` and reads the schedule it names from the event "
        "that started the run, not from the job that installs."
    ) in threat_model
    assert "Tool versions installed inside most jobs, such as Gitleaks, are explicitly selected" in posture
    assert "Two scheduled jobs do not pin on purpose." in posture
    assert "The weekly real-host canary installs the current Claude Code, Hermes, OpenCode and Codex CLIs" in posture
    assert f"{FROM_V0200} neither holds a write permission in the job that installs." in posture
    assert "Archive maintenance still holds" not in posture
