"""Search goldens (spec 3.3, slice P0g, test TG1): with every switch unset, today's outputs equal what main printed.

``fixtures_search_goldens`` holds what recall, the context pack, the brief, the ``import-markdown`` receipt and
the ``alice_memory_commit`` result printed on main before any search change, from the invented folder in
``search_goldens_corpus``. Each test here runs today's code over a fresh copy of that folder, with no ``ALICE_``
variable set, and compares. Every later search change merges with its switch off, and "off equals before" is
proved by this file passing with ``fixtures_search_goldens.py`` absent from the diff.

The tests in the second half guard the goldens themselves: a golden that was recorded again after a change
emptied a scenario would still pass the comparison, so each scenario's purpose is checked on its own.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.unit import search_goldens_support as support
from tests.unit.fixtures_search_goldens import GOLDENS

SURFACES = ("brief", "commit_result", "context_pack", "import_receipt", "recall")


def _scenarios(surface: str) -> dict[str, Any]:
    golden: Any = GOLDENS.get(surface, {})
    return dict(golden.get("scenarios", {}))


SCENARIO_IDS = [(surface, name) for surface in SURFACES for name in _scenarios(surface)]


@pytest.fixture(scope="module")
def computed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    """Today's outputs for every scenario, computed once under a clean environment."""

    return support.compute_goldens(tmp_path_factory.mktemp("search-goldens"))


def _explain(surface: str, name: str, expected: object, actual: object) -> str:
    def lines(value: object) -> list[str]:
        return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True).splitlines()

    diff = list(difflib.unified_diff(lines(expected), lines(actual), "golden", "today", lineterm="", n=1))
    shown = "\n".join(diff[:60]) + (f"\n... {len(diff) - 60} more lines" if len(diff) > 60 else "")
    return (
        f"{surface}/{name} no longer equals its golden. With every switch unset the output must be what main "
        "printed before the search release. If this change is meant to move it, record the goldens again with "
        "`PYTHONPATH=apps/api/src python -m tests.unit.search_goldens_support --commit <sha> --tag <tag> --write`, "
        "and say so in the PR; if it is not, the change moved an output it should not have.\n" + shown
    )


# ----------------------------------------------------------------------------------------------
# The comparison
# ----------------------------------------------------------------------------------------------


def test_the_goldens_cover_the_five_surfaces_and_name_the_code_they_came_from() -> None:
    """Mutation: rename a surface in the golden file, blank a ``commit``, or give a surface a tag that is no version.

    Each surface says which commit of main and which release tag it was computed from, so a later reader can
    tell what "before" means.
    """

    assert sorted(GOLDENS) == sorted(SURFACES)
    for surface, golden in GOLDENS.items():
        computed_from = golden["computed_from"]
        assert isinstance(computed_from, Mapping), surface
        assert re.fullmatch(r"[0-9a-f]{40}", str(computed_from.get("commit"))), surface
        assert re.fullmatch(r"v\d+\.\d+\.\d+", str(computed_from.get("tag"))), surface
        assert golden["scenarios"], surface


@pytest.mark.parametrize(("surface", "name"), SCENARIO_IDS, ids=[f"{s}-{n}" for s, n in SCENARIO_IDS])
def test_a_scenario_equals_its_golden(computed: dict[str, dict[str, object]], surface: str, name: str) -> None:
    """Mutation: rename a key of the recall result (``source_count``), or change one byte of a golden string.

    The same edit to ``alice_context_pack``, the brief, the ``import-markdown`` receipt or the commit result
    fails the scenarios of that surface and no other.
    """

    expected = _scenarios(surface)[name]
    # The owner's replacement ruling adds a warning for a changed path even
    # with replacement off. Keep the frozen fixture and every other byte.
    if surface == "import_receipt" and name == "edited_reimport":
        expected = {**expected, "receipt": {**expected["receipt"],
            "changed_files_count": 1,
            "replacement_hint": "Use --supersede --dry-run to preview replacement, then --supersede to apply it.",
        }}
    actual = computed[surface].get(name)
    assert actual == expected, _explain(surface, name, expected, actual)


def test_no_scenario_is_added_or_dropped(computed: dict[str, dict[str, object]]) -> None:
    """Mutation: delete a scenario from the recorder's scenario tables.

    A scenario that only one side knows about would never be compared.
    """

    for surface in SURFACES:
        assert sorted(computed[surface]) == sorted(_scenarios(surface)), surface


def test_the_golden_file_is_the_text_the_recorder_writes() -> None:
    """Mutation: add a space at the end of a line of the golden file.

    The file is recorded and never edited by hand, so its text is the recorder's rendering of its own data.
    """

    assert support.FIXTURE_PATH.read_text(encoding="utf-8") == support.render_fixture(GOLDENS)


# ----------------------------------------------------------------------------------------------
# What the run is made of
# ----------------------------------------------------------------------------------------------


def test_the_run_starts_with_every_switch_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: stop ``clean_environment`` removing the ``ALICE_`` prefix, or leave the working directory alone.

    A switch set in the shell, a host's key, the full tool surface that ``conftest`` turns on, a home folder or a
    working directory inside a checkout must not decide a golden. Everything is restored on exit.
    """

    names = {
        "ALICE_SEARCH_QUALITY": "on",
        "ALICE_MCP_COMMIT_RESULT": "compact",
        "ALICE_PROJECT_SCOPING": "on",
        "ALICE_MCP_FULL_TOOLS": "1",
        "ALICE_AGENT_API_KEY": "not-a-key",
        "ALICEBOT_EVAL_DATABASE_URL": "sqlite:///:memory:",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    before = Path.cwd()
    with support.clean_environment(tmp_path):
        assert [name for name in os.environ if name.startswith(support.ENVIRONMENT_PREFIXES)] == []
        assert not set(support.SEARCH_RELEASE_SWITCHES) & set(os.environ)
        assert os.environ["HOME"] == str(tmp_path / "home")
        assert Path.cwd() == (tmp_path / "cwd").resolve()
        assert not any((folder / ".git").exists() for folder in (Path.cwd(), *Path.cwd().parents))
    assert {name: os.environ.get(name) for name in names} == names
    assert Path.cwd() == before


def test_the_normalizer_labels_row_ids_and_blanks_every_other_id_and_stamp() -> None:
    """Mutation: skip the timestamp pattern, or leave an id the vault does not hold as it was.

    A row the vault holds keeps a label that names it, so a golden still shows which row came back. Any other id
    (a pack, a trace, a confirmation) and every ISO timestamp become a placeholder, and keys come out sorted.
    """

    row = "0b1c2d3e-0000-4000-8000-000000000001"
    other = "ffffffff-0000-4000-8000-00000000000a"
    labels = {row: "<source:runbook#1>"}
    text = f"{row} {other} policy-{other} 2026-10-02T13:52:21.792130Z 2026-10-02 13:52:21+00:00"
    assert support.normalize_text(text, labels) == "<source:runbook#1> <id> policy-<id> <ts> <ts>"
    normalized = support.normalize_value({"b": [row, {"z": other, "a": "x"}], "a": 3, "c": None}, labels)
    assert normalized == {"a": 3, "b": ["<source:runbook#1>", {"a": "x", "z": "<id>"}], "c": None}
    assert isinstance(normalized, dict) and list(normalized) == ["a", "b", "c"]


def test_where_the_vault_is_built_shows_in_no_output(computed: dict[str, dict[str, object]], tmp_path: Path) -> None:
    """Mutation: remove the ``pin_import_paths`` call from ``build_search_vault``.

    The vault is built again in a folder named with every query word of four letters or fewer, the words that
    match random text (an apostrophe splits "I'm" into ``m``). A source search matches query words against the
    stored import folder, and the pack's token estimates count it, so without the pin the number of sources a query
    lists and the estimates move with the folder's name and length. The folder is short enough for SQLite, which
    refuses a path of about 700 characters.
    """

    queries = support.scenario_queries()
    words = sorted({w for query in queries for w in re.findall(r"\w+", query.casefold()) if len(w) <= 4})
    root = tmp_path / "-".join(words)
    if len(str(root)) > 500:
        pytest.skip("the temp folder path is too long to add a folder of query words under SQLite")
    assert support.compute_goldens(root) == computed


def test_the_pinned_folder_holds_no_word_a_scenario_query_uses() -> None:
    """Mutation: pin the import folder to a path such as ``/docs/harbor-lantern``, which queries use words of.

    The pinned folder is itself searched, as text, so it must not hold a word that a scenario query holds.
    """

    pinned = support.PINNED_IMPORT_ROOT.casefold()
    used = {word for query in support.scenario_queries() for word in re.findall(r"\w+", query.casefold())}
    assert [word for word in sorted(used) if word in pinned] == []


# ----------------------------------------------------------------------------------------------
# The goldens are not empty
# ----------------------------------------------------------------------------------------------


def _facts(work: Path) -> dict[str, Any]:
    """What the search vault holds, read from its database, for the tests of what each scenario is for."""

    with support.clean_environment(work):
        vault = support.build_search_vault(work)
    connection = sqlite3.connect(str(vault.db_path))
    try:
        titles = {str(i): str(t) for i, t in connection.execute("SELECT id, title FROM sources")}
        chunks: dict[str, list[str]] = {}
        for source_id, text in connection.execute("SELECT source_id, text FROM source_chunks ORDER BY rowid"):
            chunks.setdefault(titles[str(source_id)], []).append(str(text))
        owners_of_text: dict[str, set[str]] = {}
        for title, texts in chunks.items():
            for text in texts:
                owners_of_text.setdefault(text, set()).add(title)
        labels = {
            str(title): (str(domain), str(sensitivity))
            for title, domain, sensitivity in connection.execute("SELECT title, domain, sensitivity FROM sources")
        }
        linked_memories = {
            str(key): titles[str(source_id)]
            for key, source_id in connection.execute(
                "SELECT m.memory_key, l.source_id FROM provenance_links l JOIN memories m ON m.id = l.target_id"
                " WHERE l.target_type = 'memory'"
            )
            if str(key).startswith("fact.")
        }
        stored_paths = sorted(
            {str(path) for (path,) in connection.execute("SELECT raw_path FROM sources WHERE raw_path IS NOT NULL")}
            | {
                str(json.loads(metadata).get("folder"))
                for (metadata,) in connection.execute("SELECT metadata_json FROM sources")
                if "folder" in json.loads(metadata)
            }
        )
        statuses = sorted(str(s) for (s,) in connection.execute("SELECT DISTINCT status FROM memories"))
        entities = {str(n): int(c) for n, c in connection.execute("SELECT name, mention_count FROM vnext_entities")}
    finally:
        connection.close()
    return {
        "chunk_counts": {title: len(texts) for title, texts in chunks.items()},
        "shared_text": sorted(sorted(owners) for owners in owners_of_text.values() if len(owners) > 1),
        "labels": labels,
        "stored_paths": stored_paths,
        "linked_memories": linked_memories,
        "memory_statuses": statuses,
        "entities": entities,
    }


def test_the_search_vault_holds_what_the_scenarios_are_written_for(tmp_path: Path) -> None:
    """Mutation: make the vendor note ``internal`` (no fence has anything to hold back), cut the runbook to one
    section, or remove the ``pin_import_paths`` call.

    Documents of several sections (so a question can touch more than one chunk of a document), one passage held
    by two sources, a confidential and a personal note, memories that cite a source and one that does not, a
    memory whose source was corrected afterward, a person named in two documents, and no stored path that names
    the folder the vault was built in.
    """

    facts = _facts(tmp_path / "facts")
    counts = facts["chunk_counts"]
    assert counts["runbook"] >= 5 and counts["ledger-design"] >= 3 and counts["faq"] >= 4 and counts["handbook"] == 1
    assert ["faq", "runbook"] in facts["shared_text"]
    assert facts["stored_paths"] and all(
        path.startswith(support.PINNED_IMPORT_ROOT + "/") for path in facts["stored_paths"]
    ), facts["stored_paths"]
    assert facts["labels"]["vendor-shortlist"] == ("project", "confidential")
    assert facts["labels"]["garden"] == ("personal", "private")
    assert facts["linked_memories"]["fact.release.indigo"] == "runbook"
    assert facts["linked_memories"]["fact.ledger.corrections"] == "ledger-design"
    assert "fact.style.reviews" not in facts["linked_memories"]
    assert "superseded" in facts["memory_statuses"]
    assert facts["entities"]["Orla Vance"] == 2


def _titles(result: Mapping[str, Any]) -> list[str]:
    return [str(source["title"]).strip('"') for source in result.get("sources", [])]


def _recall(name: str) -> Mapping[str, Any]:
    return _scenarios("recall")[name]["result"]


def test_the_recall_goldens_still_show_what_each_scenario_is_for() -> None:
    """Mutation: record the goldens again after a change that empties a fence scenario, such as the confidential one.

    Only facts that hold in any ranking mode are asserted, so a search change that is meant to move the order
    or the count of passages does not trip them.
    """

    assert "runbook" in _titles(_recall("sections_of_one_document"))
    assert "vendor-shortlist" not in _titles(_recall("confidential_hidden"))
    assert "vendor-shortlist" in _titles(_recall("confidential_allowed"))
    assert "garden" not in _titles(_recall("domain_project"))
    assert "garden" in _titles(_recall("domain_unfiltered"))
    assert "sources" not in _recall("sources_off")
    assert len(_titles(_recall("limit_three"))) <= 3
    assert "retrieval" in _recall("debug_trace")
    assert [entity["name"] for entity in _recall("entity_name")["entities"]] == ["Orla Vance"]
    corrected = [s for s in _recall("corrected_source")["sources"] if s.get("derived_memory_corrected")]
    assert corrected and str(corrected[0]["current_memory_id"]).startswith("<memory:")
    hopped = [m for m in _recall("hop_follows_the_first_source")["results"] if m["score"] == 0.0]
    assert hopped and all(m["provenance_count"] >= 1 for m in hopped)
    for name, entry in _scenarios("recall").items():
        if name != "confidential_hidden":  # the one scenario whose point is that nothing comes back
            assert entry["result"]["results"] or entry["result"].get("sources"), f"recall/{name} returned nothing"


def test_the_other_goldens_still_show_what_each_scenario_is_for() -> None:
    """Mutation: record the goldens again after a change that empties the pack, the brief, a receipt or a commit result.

    The pack and the brief return text from memories and sources, an import receipt counts what was imported, and
    each commit scenario ends in the status it is named for.
    """

    for name, entry in _scenarios("context_pack").items():
        result = entry["result"]
        assert result["memories"] or result["sources"], f"context_pack/{name} returned nothing"
        assert result["token_report"], name
    assert _scenarios("context_pack")["small_budget"]["result"]["token_report"]["token_estimate"] <= 500
    for name, entry in _scenarios("brief").items():
        printed = entry["printed"]
        assert printed.startswith("Stored notes from Alice memory"), name
        assert "**fact**" in printed and "**source**" in printed, name

    imports = {name: entry["receipt"] for name, entry in _scenarios("import_receipt").items()}
    assert all(entry["exit_code"] == 0 for entry in _scenarios("import_receipt").values())
    assert (imports["first_import"]["status"], imports["first_import"]["imported_count"]) == ("ok", 5)
    unchanged = imports["unchanged_reimport"]
    assert (unchanged["status"], unchanged["imported_count"]) == ("duplicate", 0)
    assert (imports["edited_reimport"]["status"], imports["edited_reimport"]["imported_count"]) == ("ok", 1)
    assert imports["copy_of_another_file"]["status"] == "duplicate"

    commits = {name: entry["result"] for name, entry in _scenarios("commit_result").items()}
    assert {name: result["status"] for name, result in commits.items()} == {
        "committed": "committed",
        "replay": "committed",
        "confirmation_required": "confirmation_required",
        "finish_confirmation": "committed",
        "review_required": "review_required",
        "committed_with_identity": "committed",
        "rejected_above_ceiling": "rejected",
        "rejected_read_only": "rejected",
    }
    assert commits["replay"]["idempotent_replay"] is True
    assert commits["confirmation_required"]["confirmation_id"]
    assert all(result["receipt"] for result in commits.values())
