"""The docs say what main does about the gaps the v0.20 wave-2 reviews found, with v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as they are. The documents that describe the
latest release mark what main changed with ``Unreleased (on main, not in v0.19.2):``,
and the changelog has one entry under the Unreleased heading that states the
v0.19.2 behaviour beside each change.

Every test names the edit that must fail it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.19.2):"
ENTRY_START = "Gaps that the reviews of the v0.20 wave-2 changes found are closed"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _entry() -> str:
    changelog = _changelog()
    unreleased = changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]
    entries = [item for item in unreleased.split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_has_one_entry_under_unreleased_and_none_above_v0192() -> None:
    """One entry, in the Unreleased section.

    Mutations, each one alone: move the entry under the v0.19.2 heading, or add a second
    entry that starts the same way.
    """
    assert _changelog().count(ENTRY_START) == 1
    assert _entry().startswith(ENTRY_START)


def test_the_entry_states_the_v0192_memory_id_behaviour() -> None:
    """The expired successor and the loop, each with what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says v0.19.2 named an id in both
    cases, or the sentence that says the status stays active after expire, or the one that
    names the loop.
    """
    entry = _entry()
    assert "`alice_memory_manage` with `action: expire` sets `valid_to` and leaves the status `active`" in entry
    assert "although `alice_recall` and `alice_context_pack` do not return it" in entry
    assert "was answered with the id of the memory the walk came back to, which is a superseded memory." in entry
    assert "In v0.19.2 both cases name an id." in entry


def test_the_pointer_residue_entry_no_longer_says_an_expired_successor_is_named() -> None:
    """The entry for the pointer residue change said an expired successor is still named. It is corrected.

    Mutation: put the sentence back.
    """
    changelog = _flat(_changelog())
    assert "a successor closed with `expire` is still named" not in changelog
    assert "A deleted successor already named no id. Second," in changelog


def test_the_tools_doc_marks_the_expired_successor_and_the_loop() -> None:
    """``current_memory_id`` is left off for a closed validity window and for a loop, and v0.19.2 is stated.

    Mutations: delete the marker, or the sentence that says what v0.19.2 names.
    """
    tools = _read("docs/alpha/mcp-tools.md")
    assert (
        MARK + " `current_memory_id` is also left off when the memory it would name has a validity window that has closed"
    ) in tools
    assert "`alice_memory_manage` with `action: expire` sets while the status stays `active`" in tools
    assert "A chain that loops back to a memory it already passed names no id." in tools
    assert "In v0.19.2 the id of the expired memory is named, and so is the id of the memory the loop came back to." in tools


def test_the_entry_states_the_v0192_backup_behaviour() -> None:
    """The four revision columns, the header and the export, each with what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says v0.19.2 stored the text, or the
    one that says the header ended with ``alice_memory_failed``, or the one that says the
    export ended with ``alice_memory_failed``.
    """
    entry = _entry()
    assert (
        "JSON text nested more than 256 levels in `previous_value`, `new_value`, `source_event_ids` and `candidate` "
        "of a memory revision"
    ) in entry
    assert "In v0.19.2 that text was stored whenever the decoder could read it (up to about 10,000 levels)" in entry
    assert "where v0.19.2 ended with `alice_memory_failed`" in entry
    assert "ends with `export_failed` after one line that names the table and the column" in entry
    assert "In v0.19.2 it ended with `alice_memory_failed`." in entry


def test_the_backup_doc_marks_the_revision_cap_the_header_and_the_export() -> None:
    """The backup guide says what main does and what v0.19.2 did for each of the three.

    Mutations: delete the marker, the v0.19.2 comparison, or the example line for the export.
    """
    text = _read("docs/alpha/backup-and-restore.md")
    assert (
        MARK + " JSON text nested more than 256 levels in the `previous_value`, `new_value`, `source_event_ids` "
        "or `candidate` column of a memory revision is refused with `restore_failed`"
    ) in text
    assert "`alice-memory: line 1: a record is nested too deeply for import to read`" in text
    assert (
        "In v0.19.2 text in those columns that the decoder could read was stored whatever its depth, and the deep "
        "header ended with `alice_memory_failed`."
    ) in text
    assert "`alice-memory: memory_revisions column previous_value is nested too deeply for export to write`" in text
    assert "ends with `export_failed`. In v0.19.2 it ended with `alice_memory_failed`." in text


def test_the_known_limitation_for_deep_backup_json_names_the_export_line() -> None:
    """The known limitation keeps the v0.19.2 sentence and adds the marked change after it.

    Mutations: delete the v0.19.2 half, the marker, or the export line.
    """
    text = _read("docs/alpha/known-limitations.md")
    assert (
        "still ends with the generic `alice_memory_failed`. " + MARK + " that export ends with `export_failed` after one "
        "line that names the table and the column"
    ) in text
    assert "`alice-memory: memory_revisions column previous_value is nested too deeply for export to write`" in text
    assert "text nested more than 256 levels in `previous_value`, `new_value`, `source_event_ids` or `candidate` is refused with `restore_failed`" in text


def test_the_entry_states_the_v0192_behaviour_of_the_three_file_commands() -> None:
    """``capture-file`` and the two ``--file`` options, with what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says v0.19.2 read the whole file, or
    the one about the log line, or the one about ``MemoryError``.
    """
    entry = _entry()
    assert (
        "`alicebot vnext sources capture-file`, `alicebot vnext connectors browser-clipper capture --file` and "
        "`alicebot vnext agents ingest-output --file` read their file with the read the importers use"
    ) in entry
    assert "`--max-file-mib N` on each of the three commands changes the limit" in entry
    assert (
        "In v0.19.2 the three read the whole file with `Path.read_text`, with no limit, followed a link put in place "
        "of the file after the path was resolved, and waited on a FIFO."
    ) in entry
    assert "logged as one line with its position, the code and the name of the error type, with no traceback" in entry
    assert "A `MemoryError` while a conversation is read ends the import" in entry
    assert "the OpenClaw default of 16 MiB is enforced" in entry


def test_the_importers_doc_covers_the_file_commands_and_the_log_line() -> None:
    """The importers guide no longer says ``capture-file`` is not covered, and says what v0.19.2 did.

    Mutations: put the old bullet back, delete the v0.19.2 comparison, or delete the log line bullet.
    """
    text = _read("docs/integrations/importers.md")
    assert "`alicebot vnext sources capture-file` is not covered" not in text
    assert (
        "read their file with the same read: once, up to 16 MiB, with the same `import_file_too_large` refusal and the "
        "same `--max-file-mib N`."
    ) in text
    assert "In v0.19.2 these three read the whole file with no limit, followed a link put in place of the file after the path was resolved, and waited on a FIFO" in text
    assert "the process log gets one line for it, with the position, the code and the name of the error type" in text
    assert "so it ends the import and is not counted as an unreadable conversation" in text


def test_the_known_limitation_and_the_readme_name_the_three_commands() -> None:
    """Both say the three commands take the limit now, and the limitation keeps the v0.19.2 sentence.

    Mutations: put ``capture-file has no limit`` back, or delete the v0.19.2 half.
    """
    limits = _read("docs/alpha/known-limitations.md")
    assert "`alicebot vnext sources capture-file` has no limit" not in limits
    assert "take the same 16 MiB limit and `--max-file-mib N`" in limits
    assert "in v0.19.2 they read the whole file with no limit." in limits
    readme = _read("README.md")
    assert (
        "`alicebot vnext sources capture-file` and the `--file` options of `alicebot vnext connectors browser-clipper "
        "capture` and `alicebot vnext agents ingest-output` take the same limit (16 MiB) and `--max-file-mib N`."
    ) in readme
