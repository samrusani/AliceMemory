"""The docs say what v0.20.0 does about the gaps the reviews of the v0.20.0 changes found, with v0.19.2 as the comparison.

v0.19.2 is released, so its notes stay as they are. The documents that describe the
latest release mark what v0.20.0 changed with ``From v0.20.0,``,
and the changelog has one entry under the v0.20.0 heading that states the
v0.19.2 behaviour beside each change.

Every test names the edit that must fail it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "From v0.20.0,"
ENTRY_START = "Gaps that the reviews of the other changes in this release found are closed"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _entry() -> str:
    changelog = _changelog()
    released = changelog[changelog.index("## v0.20.0 \u2014 2026-10-02") + len("## v0.20.0 \u2014 2026-10-02") : changelog.index("## v0.19.2")]
    entries = [item for item in released.split("\n- ")[1:] if item.startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_changelog_has_one_entry_under_v0200_and_none_in_v0192() -> None:
    """One entry, in the v0.20.0 section.

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


def test_the_entry_names_the_person_and_window_branches_of_the_metadata_scan() -> None:
    """The entry lists what the metadata scan keeps and drops under a people scope and a time window.

    Mutation: delete the clause from the entry.
    """
    assert (
        "keeps the id of a memory that is tied to the person only through the entity graph, and drops the id of a "
        "memory that the people scope or the time window rules out."
    ) in _entry()


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
    """The seven columns, the header and the export, each with what v0.19.2 did.

    Mutations, each one alone: delete the sentence that says v0.19.2 stored the text, or the
    ``value``, ``source_event_ids`` and ``aliases`` columns from the first sentence, or the
    one that says the header ended with ``alice_memory_failed``, or the one that says the
    export ended with ``alice_memory_failed``.
    """
    entry = _entry()
    assert (
        "JSON text nested more than 256 levels in `previous_value`, `new_value`, `source_event_ids` and `candidate` "
        "of a memory revision, in `value` and `source_event_ids` of a memory and in `aliases` of an entity"
    ) in entry
    assert (
        "In v0.19.2 that text was stored whenever the decoder could read it (up to about 10,000 levels) in the four "
        "revision columns, and up to about 1,000 levels in the other three"
    ) in entry
    assert "where v0.19.2 ended with `alice_memory_failed`" in entry
    assert "ends with `export_failed` after one line that names the table and the column" in entry
    assert "In v0.19.2 it ended with `alice_memory_failed`." in entry


def test_the_backup_doc_marks_the_revision_cap_the_header_and_the_export() -> None:
    """The backup guide says what v0.20.0 does and what v0.19.2 did for each of the three.

    Mutations: delete the marker, the v0.19.2 comparison, the sentence that adds the memory
    and entity columns, or the example line for the export.
    """
    text = _read("docs/alpha/backup-and-restore.md")
    assert (
        MARK + " JSON text nested more than 256 levels in the `previous_value`, `new_value`, `source_event_ids` "
        "or `candidate` column of a memory revision is refused with `restore_failed`"
    ) in text
    assert (
        "and so is such text in the `value` or `source_event_ids` column of a memory and the `aliases` column of an "
        "entity."
    ) in text
    assert "`alice-memory: line 1: a record is nested too deeply for import to read`" in text
    assert (
        "In v0.19.2 text in the four revision columns that the decoder could read was stored whatever its depth, "
        "text in the other three was stored up to about 1,000 levels, and the deep header ended with "
        "`alice_memory_failed`."
    ) in text
    assert "`alice-memory: memory_revisions column previous_value is nested too deeply for export to write`" in text
    assert "ends with `export_failed`. In v0.19.2 it ended with `alice_memory_failed`." in text


def test_the_known_limitation_for_deep_backup_json_names_the_export_line() -> None:
    """The known limitation keeps the v0.19.2 sentence and adds the marked change after it.

    Mutations: delete the v0.19.2 half, the marker, the export line, or the memory and entity
    columns from the last sentence.
    """
    text = _read("docs/alpha/known-limitations.md")
    assert (
        "still ends with the generic `alice_memory_failed`. " + MARK + " that export ends with `export_failed` after one "
        "line that names the table and the column"
    ) in text
    assert "`alice-memory: memory_revisions column previous_value is nested too deeply for export to write`" in text
    assert (
        "text nested more than 256 levels in `previous_value`, `new_value`, `source_event_ids` or `candidate` of a "
        "revision, in `value` or `source_event_ids` of a memory, or in `aliases` of an entity is refused with "
        "`restore_failed`"
    ) in text


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


def test_the_entry_states_the_v0192_local_folder_behaviour() -> None:
    """The listing cap, ``ignored_count`` and the two printed numbers, with what v0.19.2 did.

    Mutations, each one alone: delete the sentence about ``.github``, the one about
    ``ignored_count``, or the one that says neither number existed in v0.19.2.
    """
    entry = _entry()
    assert "the scan does not enter a folder whose name is on the default ignore list" in entry
    assert "A folder that only starts like an ignored name, such as `.github`, is entered." in entry
    assert "Files inside an ignored folder are no longer counted in `ignored_count`" in entry
    assert "In v0.19.2 the scan listed and sorted every entry of the walk, ignored folders included" in entry
    assert "`alicebot vnext connectors local-folder sync` and `watch` print `refused_count` and `truncated`" in entry
    assert "In v0.19.2 neither number existed." in entry
    assert "come from the descriptor that was read, whether the file changes before the read or after it." in entry


def test_the_known_limitation_and_the_threat_model_say_ignored_folders_are_not_entered() -> None:
    """Both keep the 100,000 figure and say what it counts, and the limitation names the printed numbers.

    Mutations: delete either sentence, or the marker the limitation's paragraph sits under.
    """
    raw = (ROOT / "docs/alpha/known-limitations.md").read_text(encoding="utf-8")
    (bullet,) = [item for item in raw.split("\n- ") if item.startswith("in v0.19.2 the local-folder scan reads each matching file whole")]
    assert bullet.index(MARK) < bullet.index("It does not enter a folder named like a default ignore")
    limits = _flat(bullet)
    assert (
        "It does not enter a folder named like a default ignore (`node_modules`, `.git`, `.venv` and the rest of the "
        "list, compared without regard to case), so the inside of one neither costs time nor counts toward the "
        "100,000, and its files are not in `ignored_count`."
    ) in limits
    assert "`alicebot vnext connectors local-folder sync` and `watch` print `refused_count` and `truncated`." in limits
    threat = _read("docs/security/threat-model.md")
    assert (
        "lists at most 100,000 directory entries without entering a folder named like a default ignore such as "
        "`node_modules` or `.git`"
    ) in threat


def test_the_security_docs_say_what_the_importers_do_not_what_they_did_before_v0152() -> None:
    """The threat model and the input guide describe the importers as they are, with the two residuals.

    The hard link residual is checked against the code too: ``read_contained_source_text`` names it
    as a known limitation. The refusal the docs describe is in the test below.

    Mutations: put either old sentence back, delete a residual, or delete the dated correction.
    """
    threat = _read("docs/security/threat-model.md")
    assert "can follow an outside-root symlink" not in threat
    assert "can reread a file after archiving it. Until remediated" not in threat
    assert (
        "The Markdown, ChatGPT, and OpenClaw directory importers refuse a symlinked file or folder under the selected "
        "root and a path that leaves it, read each file once, and archive the text they parse (since v0.15.2)."
    ) in threat
    assert "A hard link planted in the folder to a file elsewhere is read as ordinary content" in threat
    assert "a folder on the path swapped for a symlink between the listing and the read can redirect the read" in threat
    assert "Corrected 2026-10-02: this entry said an importer could follow an outside-root symlink" in threat

    guide = _read("docs/security/input-validation.md")
    assert "can currently include a symlinked member outside the selected root" not in guide
    assert "Since v0.15.2 those importers refuse a symlinked file or folder under the selected root" in guide
    assert "A hard link planted in the folder to a file elsewhere is read as ordinary content" in guide
    assert "A folder on the path swapped for a symlink between the listing and the read can redirect the read" in guide
    assert "Corrected 2026-10-02: this section said the importers could include a symlinked member" in guide

    from alicebot_api import importer_paths

    assert "a hard link planted inside the root" in " ".join((importer_paths.read_contained_source_text.__doc__ or "").split())


def test_the_entry_says_the_importer_docs_were_corrected_and_what_v0192_ships() -> None:
    """The entry names both files, v0.15.2, the two residuals and what v0.19.2 ships.

    Mutations, each one alone: delete the v0.19.2 sentence, or the sentence that names the residuals.
    """
    entry = _entry()
    assert "That has not been true since v0.15.2" in entry
    assert "a hard link planted inside the selected folder and an ancestor folder swapped for a symlink" in entry
    assert "v0.19.2 ships the old text in both files." in entry


def test_the_importers_refuse_a_symlinked_file_and_a_symlinked_folder(tmp_path: Path) -> None:
    """What the docs now say the importers do, shown on the code that lists and reads a source.

    A link to a file outside the selected root, and a link to a folder outside it, are refused when
    the folder is listed, and a link in place of a listed file is refused when it is opened.

    Mutations: drop the file ``is_symlink`` refusal or the directory one in
    ``contained_source_files``, or open without ``O_NOFOLLOW`` in ``read_contained_source_text``.
    (``followlinks=False`` on the walk is not a separate mutation: the directory refusal fires
    before a followed link would be entered.)
    """
    import pytest

    from alicebot_api.importer_paths import contained_source_files, read_contained_source_text

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("- Note: outside the root\n", encoding="utf-8")

    linked_file_root = tmp_path / "linked-file"
    linked_file_root.mkdir()
    (linked_file_root / "a.md").symlink_to(outside / "secret.md")
    with pytest.raises(ValueError, match="symlinked files"):
        contained_source_files(linked_file_root, suffixes=(".md",), recursive=True, error_factory=ValueError)

    linked_folder_root = tmp_path / "linked-folder"
    linked_folder_root.mkdir()
    (linked_folder_root / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked directories"):
        contained_source_files(linked_folder_root, suffixes=(".md",), recursive=True, error_factory=ValueError)

    swapped_root = tmp_path / "swapped"
    swapped_root.mkdir()
    note = swapped_root / "note.md"
    note.write_text("- Note: a plain note\n", encoding="utf-8")
    listed = contained_source_files(swapped_root, suffixes=(".md",), recursive=True, error_factory=ValueError)
    note.unlink()
    note.symlink_to(outside / "secret.md")
    with pytest.raises(ValueError, match="symlinked files"):
        read_contained_source_text(listed[0], source_root=swapped_root, max_bytes=1000, error_factory=ValueError)
