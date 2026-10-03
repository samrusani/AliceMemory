"""The method note and the CHANGELOG entry of the search-quality harness.

The note says what the harness measures and shows how to run it, and it is marked as main-only
until a release carries it. These tests keep the note honest: its marker is in the note itself,
every command it shows still parses against the real command line, and the CHANGELOG entry that
states the v0.20.0 behaviour is there.

Every test names the mutation that must fail it.
"""

from __future__ import annotations

import json
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


# The files the search-quality harness adds, which are public prose or code that anyone reads.
ADDED_FILES = (
    "docs/benchmarks/agent-answer/README.md",
    "gates.json",
    "scripts/alice_bench.py",
    "scripts/alice_bench_gates.py",
    "scripts/alice_bench_audit.py",
)
VENDOR_NAMES = (
    "Claude",
    "Anthropic",
    "OpenAI",
    "Codex",
    "Gemini",
    "Copilot",
    "Cursor",
    "Sonnet",
    "Opus",
    "GPT",
)
EM_DASH, EN_DASH = "\u2014", "\u2013"


def _added_texts() -> dict[str, str]:
    texts = {name: (REPO_ROOT / name).read_text(encoding="utf-8") for name in ADDED_FILES}
    fixtures = REPO_ROOT / "tests" / "fixtures" / "search_quality"
    for path in sorted(fixtures.rglob("*")):
        if path.is_file():
            texts[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return texts


def _changelog_entry() -> str:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## ", 2)[1]
    entries = [entry for entry in unreleased.split("\n- ") if "scripts/alice_bench.py" in entry]
    assert len(entries) == 1
    return entries[0]


CLAIMS = ("independently audited", "third-party audited", "penetration tested")


def _violations(text: str) -> list[str]:
    found: list[str] = []
    if EM_DASH in text or EN_DASH in text:
        found.append("dash")
    if re.search(r"(?:/Users|/home)/", text):
        found.append("local path")
    found.extend(f"vendor {name}" for name in VENDOR_NAMES if re.search(rf"\b{name}\b", text))
    found.extend(f"claim {claim}" for claim in CLAIMS if claim in text)
    return found


def test_the_scan_sees_each_kind_of_thing_it_looks_for() -> None:
    """The rule below is not vacuous: each kind of violation is flagged in a planted string, a clean one is not.

    Mutation: drop the dash, path, vendor or claim test from ``_violations``, or make the vendor test
    match nothing (an empty ``VENDOR_NAMES``).
    """

    assert _violations("plain words about notes and anchors") == []
    assert _violations(f"a {EM_DASH} b") == ["dash"] and _violations(f"a {EN_DASH} b") == ["dash"]
    mac_home, linux_home = "/" + "Users", "/" + "home"  # joined here so this file holds no home path itself
    assert _violations(f"see {mac_home}/ada/notes") == ["local path"]
    assert _violations(f"see {linux_home}/ada/notes") == ["local path"]
    for name in VENDOR_NAMES:
        assert _violations(f"reviewed by {name} today") == [f"vendor {name}"]
    assert _violations("never independently audited") == ["claim independently audited"]
    assert _violations("a cursor moves") == [], "a vendor name is matched as a whole word"


def test_the_added_files_have_no_dashes_local_paths_vendor_names_or_audit_claims() -> None:
    """Plain prose in every public file this work adds: no em or en dash, no home path, no outside tool or vendor.

    Mutation: add a dash character, a home path, one of the vendor names of ``VENDOR_NAMES`` or a claim
    of an outside audit to the note, a script, ``gates.json``, a fixture or the CHANGELOG entry.
    """

    texts = _added_texts()
    texts["CHANGELOG entry"] = _changelog_entry()
    assert len(texts) >= 6 + 8, "the note, the scripts, gates.json, the fixture files and the entry are all read"
    for name, text in texts.items():
        assert _violations(text) == [], name
    assert EM_DASH not in Path(__file__).read_text(encoding="utf-8"), "this file spells dashes as escapes"


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


def test_the_note_names_what_a_vault_is_held_to_and_each_name_is_a_real_manifest_key(tmp_path: Path) -> None:
    """The note says how a vault is tied to its checkout, folder, contents and snapshot, in the keys the harness writes.

    It also says that git is read only when there is a ``.git``, because the first version of the note said a run
    needs git and an exported copy of a commit has none.

    Mutation: rename ``vault_dir``, ``vault_text_sha256``, ``vault_chunks_sha256``, ``vault_row_counts``,
    ``harness_sha256`` or ``checkout_source_sha256`` in the harness without the note, delete the ``no git``
    sentence, put back the sentence that a run needs git, drop the sentence that a stray file in the snapshot is
    refused, or drop the source hash, the ``no git`` statement or the vault folder from the CHANGELOG entry.
    """

    text = _note()
    run_dir = tmp_path / "run"
    corpus = REPO_ROOT / "tests" / "fixtures" / "search_quality" / "corpus"
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(corpus)]) == 0
    manifest = json.loads((run_dir / bench.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    for key in ("vault_dir", "snapshot_hash", "harness_sha256", *bench.VAULT_IDENTITY_KEYS):
        assert key in manifest and f"`{key}`" in text, key
    for key in ("checkout_source_sha256", "python_version", "sqlite_version"):
        assert key in manifest["build"] and f"`{key}`" in text, key
    assert "a `.DS_Store` that a Finder visit adds is a refusal too" in text
    assert "so an export that sits inside another repository is never given that repository's commit" in text
    assert "`no git`" in text and "reads git only when the checkout has a `.git` of its own" in text
    assert "and git, because a run reads the commit of its checkout" not in text
    entry = _changelog_entry()
    assert "a hash of the source under `apps/api/src`" in entry and "recorded as `no git`" in entry
    assert "the folder it was built into and what it holds (its sources, the text of every chunk and the row count of every table)" in entry


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
    """One Unreleased entry says what the harness is and what v0.20.0 had instead, and says it truthfully.

    v0.20.0 has no agent-answer harness and no agent-answer score, but it does publish the LongMemEval
    receipt and ships a ``retrieval_quality`` eval suite, so the entry names both rather than claim that
    nothing existed.

    Mutation: delete the entry, drop its last sentence about v0.20.0, or write that the LongMemEval
    numbers were v0.20.0's only retrieval numbers (the eval suite exists at the tag).
    """

    entry = _changelog_entry()
    assert "gates.json" in entry and "docs/benchmarks/agent-answer/README.md" in entry
    assert "A vault records which checkout built it" in entry
    tail = entry[entry.index("v0.20.0 has no agent-answer harness") :]
    assert "v0.20.0 has no agent-answer harness and states no agent-answer score" in tail
    assert "LongMemEval receipt" in tail and "`retrieval_quality` eval suite" in tail
    assert "only retrieval numbers" not in entry and "no such harness" not in entry


def test_the_note_states_the_ci_time_sample_that_gates_json_records() -> None:
    """The CI time sentence of the note carries the numbers of ``gates.json`` and no others.

    Mutation: change the median, the maximum, the number of runs or the two dates of the pre-split
    sample in the note, change the shard median, maximum or run count in the note, or change any of
    them in ``gates.json`` without the note.
    """

    import json

    budget = json.loads((REPO_ROOT / "gates.json").read_text(encoding="utf-8"))["ci_time"]
    before = budget["before_split"]
    sentence = next(line for line in _note().splitlines() if line.startswith("The same file records the budget"))
    before_median = f"{before['measured_seconds']['median'] / 60:.1f}"
    before_max = f"{before['measured_seconds']['max'] / 60:.1f}"
    shard_median = f"{budget['measured_seconds']['median'] / 60:.1f}"
    shard_max = f"{budget['measured_seconds']['max'] / 60:.1f}"
    assert f"The unit tests run as {budget['shard_count']} parallel shard jobs" in sentence
    assert f"each with a {budget['timeout_minutes']} minute limit" in sentence
    assert (
        f"a median of {before_median} and at most {before_max} minutes across the "
        f"{before['measured_runs']} successful runs of 2026-10-02 and 2026-10-03"
    ) in sentence
    assert (
        f"the longest shard ran a median of {shard_median} and at most {shard_max} minutes across the "
        f"{budget['measured_runs']} successful runs of the pull request that split it"
    ) in sentence
    assert f"may add {budget['new_test_budget_seconds']} seconds" in sentence
    assert f"if a shard passes {budget['split_threshold_minutes']} minutes" in sentence
