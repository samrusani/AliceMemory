"""The method note and the CHANGELOG entry of the search-quality harness.

The note says what the harness measures and shows how to run it, and it is marked as main-only
until a release carries it. These tests keep the note honest: its marker is in the note itself,
every command it shows still parses against the real command line, and the CHANGELOG entry that
states the v0.20.0 behaviour is there.

Every test names the mutation that must fail it.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import scripts.alice_bench as bench
import scripts.alice_bench_gates as gate_sim

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTE = REPO_ROOT / "docs" / "benchmarks" / "agent-answer" / "README.md"
MARKER = "Unreleased (on main, not in v0.20.0):"


def _note() -> str:
    return NOTE.read_text(encoding="utf-8")


def test_the_method_note_carries_its_own_main_only_marker_once() -> None:
    """The note describes behaviour that is on main and not in v0.20.0, and says so in its own first paragraph.

    Mutation: delete the marker from the note, or add a second one. The count is taken inside this
    file, never over a docs page, because other pull requests add marked bullets to other pages.
    """

    text = _note()
    assert text.count(MARKER) == 1
    first_paragraph = text.split("\n\n")[1]
    assert first_paragraph.startswith(MARKER)
    assert "v0.20.0 has no harness for it and no agent-answer number" in first_paragraph


def test_the_method_note_has_no_dashes_paths_or_vendor_names() -> None:
    """Plain prose: no em or en dashes, no local paths, no name of an outside review tool.

    Mutation: add a dash character, a home path or a vendor name to the note.
    """

    text = _note()
    assert "—" not in text and "–" not in text
    assert not re.search(r"(?:/Users|/home)/", text)
    for forbidden in ("independently audited", "third-party audited", "penetration tested"):
        assert forbidden not in text


def test_every_command_the_note_shows_parses_against_the_real_command_line() -> None:
    """A renamed flag or subcommand breaks the note, so the note cannot drift from the harness.

    Mutation: rename ``--grep-options`` or ``--strict-flags`` in ``alice_bench.py``, or drop the
    ``fingerprint`` subcommand.
    """

    text = _note()
    shown = re.findall(r"^python scripts/(alice_bench\w*)\.py ?(.*)$", text, flags=re.MULTILINE)
    assert len(shown) >= 10
    seen_commands: set[str] = set()
    for script, arguments in shown:
        argv = shlex.split(arguments)
        if script == "alice_bench":
            bench.build_parser().parse_args(argv)
            seen_commands.add(argv[0])
        elif script == "alice_bench_gates":
            gate_sim.build_parser().parse_args(argv)
        else:
            raise AssertionError(f"the note shows a command for an unknown script: {script}")
    assert seen_commands == {"build", "batch", "score", "recall", "anchors", "fingerprint", "search"}


def test_the_note_names_the_files_a_reader_needs() -> None:
    """The note points at the scripts, the lock file and the public fixture, and each exists.

    Mutation: move or rename one of them without editing the note.
    """

    text = _note()
    for path in (
        "scripts/alice_bench.py",
        "scripts/alice_bench_gates.py",
        "scripts/alice_bench_audit.py",
        "gates.json",
        "tests/fixtures/search_quality",
    ):
        assert path in text
        assert (REPO_ROOT / path).exists(), path


def test_the_changelog_entry_states_the_v0200_behaviour() -> None:
    """One Unreleased entry says what the harness is and what v0.20.0 had instead.

    Mutation: delete the entry, or drop its last sentence about v0.20.0.
    """

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## ", 2)[1]
    assert unreleased.startswith("Unreleased")
    entries = [entry for entry in unreleased.split("\n- ") if "scripts/alice_bench.py" in entry]
    assert len(entries) == 1
    assert "v0.20.0 has no such harness" in entries[0]
    assert "gates.json" in entries[0] and "docs/benchmarks/agent-answer/README.md" in entries[0]
