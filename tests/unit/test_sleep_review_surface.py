"""Review surface for sleep proposals: listing, doctor, cap lines, slot release.

Each test names the edit that makes it fail. Credential values are built
at runtime.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from alicebot_api.credential_floor import credential_verdict
from alicebot_api.legacy_credential_check import commit_door_secret_verdict, commit_gate_refuses
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import (
    _KNOWN_COMMANDS,
    bootstrap_database,
    build_parser,
    main as onramp_main,
    resolve_db_path,
    sqlite_url_for_path,
)
from alicebot_api.session_briefing import SESSION_BRIEF_FRAME, compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vault_doctor import compile_local_vault_doctor
from alicebot_api.vault_sleep import (
    NO_SLEEP_PROPOSALS,
    SLEEP_EXCERPT_MAX,
    SLEEP_PROPOSAL_CAP,
    SleepError,
    _write_jsonl,
    compile_sleep_proposal_listing,
    load_sleep_proposals,
    run_local_vault_sleep,
    sleep_proposals_path,
)
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
)

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
OPEN_FENCE = {
    "effective_domains": (),
    "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"),
    "effective_project_scope": (),
}


def _database(tmp_path: Path, user_id: str = USER_ID) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=user_id, user_email="local@alice")
    return database


def _create_source(
    store: SQLiteVNextStore,
    *,
    note: str,
    suffix: str,
    minute: int,
    domain: str = "project",
    sensitivity: str = "public",
    project: str = "harbour",
) -> dict[str, object]:
    source = store.create_source(
        {
            "source_type": "note",
            "title": f"Harbour note {suffix}",
            "content_hash": f"hash-review-{suffix}",
            "captured_at": f"2026-08-01T08:{minute:02d}:00Z",
            "domain": domain,
            "sensitivity": sensitivity,
            "metadata_json": {"project_scope": [project], "raw_text": note},
        }
    )
    store.create_source_chunk(
        {
            "source_id": source["id"],
            "chunk_index": 0,
            "text": note,
            "token_count": max(1, len(note.split())),
        }
    )
    return source


def _line_value(report: str, label: str) -> str:
    prefix = f"{label}: "
    for line in report.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing {label!r} in report:\n{report}")


def _legacy_assignment() -> str:
    return "PASSWORD" + "_DB=" + "Ab" + "12" + "cd" + "EF"


def _floor_token() -> str:
    return "ghp_" + "ab12cd34ef56"


def _commit_source(database: Path, source_id: str, excerpt: str) -> None:
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        memory = store.create_memory(
            {
                "memory_key": f"sleep.review.{source_id[:8]}",
                "memory_type": "decision",
                "title": excerpt[:120],
                "canonical_text": excerpt,
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "project_scope": ["harbour"],
                "metadata_json": {"project_scope": ["harbour"], "source_refs": [source_id]},
                "value": {"text": excerpt, "source_refs": [source_id]},
            }
        )
        store.create_provenance_link(
            {
                "target_type": "memory",
                "target_id": str(memory["id"]),
                "source_id": source_id,
                "evidence_role": "supports",
                "confidence": 0.9,
            }
        )


def _seed_unlinked(database: Path, count: int, *, user_id: str, suffix: str) -> list[str]:
    source_ids: list[str] = []
    with sqlite_user_connection(database, user_id) as connection:
        if user_id != USER_ID:
            ensure_sqlite_user(connection, user_id, "other@alice", "Other")
        store = SQLiteVNextStore(connection, user_id)
        for index in range(count):
            source = _create_source(
                store,
                note=f"Unlinked harbour clipboard note {suffix} {index}.",
                suffix=f"{suffix}{index}",
                minute=index + 1,
            )
            source_ids.append(str(source["id"]))
    return source_ids


def test_listing_fence_parameters_have_no_default() -> None:
    """Mutation: default the fence kwargs. A caller can omit the brief's controls."""

    signature = inspect.signature(compile_sleep_proposal_listing)
    for name in (
        "effective_domains",
        "effective_sensitivity_allowed",
        "effective_project_scope",
    ):
        assert signature.parameters[name].default is inspect.Parameter.empty


def test_cap_stays_full_until_a_source_has_an_active_memory(tmp_path: Path) -> None:
    """Ten unlinked notes. Run 1 writes 8 and prints the cap-full lines.

    Before the slot release, committing two excerpts left run 2 at 0 writes.
    This test fails if those two sources still occupy cap slots.
    """

    database = _database(tmp_path)
    source_ids = _seed_unlinked(database, 10, user_id=USER_ID, suffix="ten")
    first = run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    rows = load_sleep_proposals(sidecar)
    assert int(_line_value(first, "proposals written")) == SLEEP_PROPOSAL_CAP
    assert int(_line_value(first, "sources not proposed")) == 2
    assert "sleep_proposals.jsonl" in first
    assert str(sidecar) in first
    assert len(rows) == SLEEP_PROPOSAL_CAP
    proposed = [str(row["source_id"]) for row in rows]
    assert proposed == source_ids[:SLEEP_PROPOSAL_CAP]

    held = {row["source_id"]: dict(row) for row in rows}
    _commit_source(database, proposed[0], str(rows[0]["excerpt"]))
    _commit_source(database, proposed[1], str(rows[1]["excerpt"]))
    second = run_local_vault_sleep(database, user_id=USER_ID)
    after = load_sleep_proposals(sidecar)
    assert int(_line_value(second, "proposals written")) == 2
    assert "sources not proposed:" not in second
    assert [str(row["source_id"]) for row in after] == source_ids
    for source_id in proposed[:2]:
        match = next(row for row in after if row["source_id"] == source_id)
        assert match == held[source_id]


def test_a_second_user_can_fill_eight_while_the_first_cap_is_full(tmp_path: Path) -> None:
    """Mutation: count every user's rows toward the cap. The second user writes 0."""

    database = _database(tmp_path)
    _seed_unlinked(database, 10, user_id=USER_ID, suffix="first")
    _seed_unlinked(database, 8, user_id=OTHER_USER_ID, suffix="second")
    first = run_local_vault_sleep(database, user_id=USER_ID)
    second = run_local_vault_sleep(database, user_id=OTHER_USER_ID)
    rows = load_sleep_proposals(sleep_proposals_path(database))
    assert int(_line_value(first, "proposals written")) == 8
    assert int(_line_value(second, "proposals written")) == 8
    assert sum(1 for row in rows if row["user_id"] == USER_ID) == 8
    assert sum(1 for row in rows if row["user_id"] == OTHER_USER_ID) == 8


def test_a_full_cap_with_no_committed_source_writes_nothing_on_the_second_run(tmp_path: Path) -> None:
    """Mutation: count only this run's rows, or drop the cap-full line."""

    database = _database(tmp_path)
    _seed_unlinked(database, 10, user_id=USER_ID, suffix="hold")
    run_local_vault_sleep(database, user_id=USER_ID)
    second = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(second, "proposals written")) == 0
    assert int(_line_value(second, "sources not proposed")) == 2
    assert "active or accepted memory" in second


def test_listing_applies_each_brief_control_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """Framing, quoting, fences, the other user, and the commit door.

    Mutation: skip one fence, print another user's row, or call _write_jsonl.
    """

    database = _database(tmp_path)
    public = "The public harbour checklist stays on channel 7."
    private = "The private harbour salary note stays off the public brief."
    personal = "The personal harbour domain note is not a project note."
    other_project = "The other project harbour note stays in its own scope."
    newline = "keep\nthe harbour radio on one quoted line"
    legacy = f"we decided {_legacy_assignment()} for the billing store"
    floor = f"rotate {_floor_token()} before the deploy"
    assert credential_verdict(legacy) is None
    assert commit_gate_refuses("", legacy) is True
    assert credential_verdict(floor) is not None

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        public_id = str(
            _create_source(store, note=public, suffix="public", minute=1)["id"]
        )
        private_id = str(
            _create_source(
                store,
                note=private,
                suffix="private",
                minute=2,
                sensitivity="private",
                domain="project",
            )["id"]
        )
        personal_id = str(
            _create_source(
                store,
                note=personal,
                suffix="personal",
                minute=3,
                domain="personal",
            )["id"]
        )
        other_id = str(
            _create_source(
                store,
                note=other_project,
                suffix="otherproj",
                minute=4,
                project="other",
            )["id"]
        )
        newline_id = str(_create_source(store, note=newline, suffix="newline", minute=5)["id"])
        legacy_id = str(_create_source(store, note=legacy, suffix="legacy", minute=6)["id"])
        floor_id = str(_create_source(store, note=floor, suffix="floor", minute=7)["id"])

    run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    rows = load_sleep_proposals(sidecar)
    rows.append(
        {
            "excerpt": "another user's harbour note",
            "source_id": public_id,
            "status": "proposed",
            "user_id": OTHER_USER_ID,
        }
    )
    sidecar.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    before = sidecar.read_bytes()
    memories_before = _memory_count(database)

    def _boom(*_args, **_kwargs) -> None:
        raise AssertionError("listing wrote the sidecar")

    monkeypatch.setattr("alicebot_api.vault_sleep._write_jsonl", _boom)
    open_listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    assert open_listing.splitlines()[0] == SESSION_BRIEF_FRAME
    assert public in open_listing
    assert private in open_listing
    assert personal in open_listing
    assert other_project in open_listing
    assert "another user's harbour note" not in open_listing
    assert _legacy_assignment() not in open_listing
    assert _floor_token() not in open_listing
    assert f"source_id: {public_id}" in open_listing
    excerpt_line = next(line for line in open_listing.splitlines() if newline_id and "keep the harbour radio" in line)
    assert excerpt_line.startswith("excerpt: ")
    assert "\n" not in excerpt_line
    assert "keep the harbour radio" in excerpt_line
    commit_line = next(
        line for line in open_listing.splitlines() if line.startswith("alice_memory_commit: ") and public_id in line
    )
    arguments = json.loads(commit_line.split(": ", 1)[1])
    assert arguments["canonical_text"] == public
    assert arguments["source_refs"] == [public_id]
    assert arguments["title"] == public[:120]
    assert arguments["domain"] == "project"
    assert arguments["sensitivity"] == "public"
    assert arguments["project_scope"] == ["harbour"]
    first_source = next(line for line in open_listing.splitlines() if line.startswith("source_id: "))
    assert first_source == f"source_id: {public_id}"

    public_only = compile_sleep_proposal_listing(
        database,
        user_id=USER_ID,
        effective_domains=(),
        effective_sensitivity_allowed=("public",),
        effective_project_scope=(),
    )
    assert private not in public_only
    assert public in public_only
    assert private_id not in public_only

    project_domain = compile_sleep_proposal_listing(
        database,
        user_id=USER_ID,
        effective_domains=("project",),
        effective_sensitivity_allowed=("public", "internal", "private", "unknown"),
        effective_project_scope=(),
    )
    assert personal not in project_domain
    assert personal_id not in project_domain
    assert public in project_domain

    harbour_only = compile_sleep_proposal_listing(
        database,
        user_id=USER_ID,
        effective_domains=(),
        effective_sensitivity_allowed=("public", "internal", "private", "unknown"),
        effective_project_scope=("harbour",),
    )
    assert other_project not in harbour_only
    assert other_id not in harbour_only
    assert public in harbour_only

    assert sidecar.read_bytes() == before
    assert _memory_count(database) == memories_before
    assert legacy_id not in open_listing
    assert floor_id not in open_listing
    assert newline_id in open_listing


def test_listing_orders_by_captured_at_when_append_order_is_reversed(tmp_path: Path) -> None:
    """A later import with an earlier captured_at is listed first.

    Mutation: print sidecar rows in file order. The earlier source lands last.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        imported_first = _create_source(
            store,
            note="The later harbour note was imported first.",
            suffix="latercap",
            minute=40,
        )
    first = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(first, "proposals written")) == 1
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        imported_second = _create_source(
            store,
            note="The earlier harbour note was imported second.",
            suffix="earliercap",
            minute=5,
        )
    second = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(second, "proposals written")) == 1
    sidecar_ids = [
        str(row["source_id"]) for row in load_sleep_proposals(sleep_proposals_path(database))
    ]
    later_id = str(imported_first["id"])
    earlier_id = str(imported_second["id"])
    assert sidecar_ids == [later_id, earlier_id]
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    source_lines = [line for line in listing.splitlines() if line.startswith("source_id: ")]
    assert source_lines == [f"source_id: {earlier_id}", f"source_id: {later_id}"]


def test_listing_of_an_empty_sidecar_is_one_quiet_line(tmp_path: Path) -> None:
    database = _database(tmp_path)
    assert compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE) == NO_SLEEP_PROPOSALS


def test_cli_lists_proposals_and_doctor_counts_them_apart_from_candidates(tmp_path: Path, capsys) -> None:
    """Doctor's sleep line is not candidates waiting. Mutation: reuse that count."""

    assert "sleep-proposals" in _KNOWN_COMMANDS
    help_text = build_parser().format_help()
    assert "oldest" in help_text
    database = _database(tmp_path)
    _seed_unlinked(database, 3, user_id=USER_ID, suffix="cli")
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        store.create_memory(
            {
                "memory_key": "sleep.review.candidate",
                "memory_type": "semantic",
                "title": "Unreviewed rumour",
                "canonical_text": "Do not trust the unreviewed harbour rumour.",
                "status": "candidate",
                "domain": "project",
                "sensitivity": "public",
                "project_scope": ["harbour"],
                "metadata_json": {"project_scope": ["harbour"]},
                "value": {"text": "Do not trust the unreviewed harbour rumour."},
            }
        )
    before = compile_local_vault_doctor(database, user_id=USER_ID)
    assert _line_value(before, "candidates waiting") == "1"
    assert _line_value(before, "sleep proposals") == "0"
    assert onramp_main(["sleep", "--data-dir", str(tmp_path), "--user-id", USER_ID]) == 0
    capsys.readouterr()
    assert onramp_main(["sleep-proposals", "--data-dir", str(tmp_path), "--user-id", USER_ID]) == 0
    listed = capsys.readouterr().out
    assert listed.startswith(SESSION_BRIEF_FRAME)
    assert "alice_memory_commit" in listed
    after = compile_local_vault_doctor(database, user_id=USER_ID)
    assert _line_value(after, "candidates waiting") == "1"
    assert _line_value(after, "sleep proposals") == "3"


def test_help_receipt_and_docstrings_do_not_say_committing_frees_a_slot() -> None:
    """The release describes when a row stops counting. It does not say committing frees a slot."""

    import alicebot_api.onramp as onramp
    import alicebot_api.vault_doctor as doctor
    import alicebot_api.vault_sleep as sleep

    blob = "\n".join(
        (
            sleep.__doc__ or "",
            inspect.getsource(sleep.format_sleep_receipt),
            inspect.getsource(sleep.run_local_vault_sleep),
            inspect.getsource(sleep.compile_sleep_proposal_listing),
            doctor.__doc__ or "",
            onramp.build_parser().format_help(),
        )
    )
    lowered = blob.casefold()
    assert "frees a slot" not in lowered
    assert "committing frees" not in lowered


def test_private_commit_arguments_stay_out_of_the_public_unknown_brief(
    tmp_path: Path, monkeypatch
) -> None:
    """A private source stays hidden under ('public', 'unknown').

    The printed arguments are what alice_memory_commit stores. Mutation:
    omit domain, sensitivity, or project_scope. The fact is stored as
    unknown and the narrow brief shows it.
    """

    private = "The private harbour salary note stays off the public brief."
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(
            store,
            note=private,
            suffix="salary",
            minute=3,
            domain="personal",
            sensitivity="private",
            project="salary-desk",
        )
    source_id = str(source["id"])
    run_local_vault_sleep(database, user_id=USER_ID)
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    commit_line = next(
        line
        for line in listing.splitlines()
        if line.startswith("alice_memory_commit: ") and source_id in line
    )
    arguments = json.loads(commit_line.split(": ", 1)[1])
    from alicebot_api.mcp.registry import call_mcp_tool

    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    payload = call_mcp_tool(context, name="alice_memory_commit", arguments=arguments)
    assert payload["status"] == "committed", payload
    memory = payload["memory"]
    assert memory["domain"] == "personal"
    assert memory["sensitivity"] == "private"
    assert memory["project_scope"] == ["salary-desk"]

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        narrow = compile_session_brief(
            store,
            effective_domains=(),
            effective_sensitivity_allowed=("public", "unknown"),
            effective_project_scope=(),
            query=None,
        )
        wide = compile_session_brief(
            store,
            effective_domains=(),
            effective_sensitivity_allowed=("public", "unknown", "private"),
            effective_project_scope=(),
            query=None,
        )
    fact_line = f"**fact**: {json.dumps(private)}"
    assert fact_line not in narrow
    assert private not in narrow
    assert fact_line in wide


def test_listing_does_not_offer_a_source_that_already_has_a_fact(tmp_path: Path, monkeypatch) -> None:
    """An accepted proposal is not printed as a fresh commit.

    Mutation: list the sidecar row again after the source has an active
    memory. The commit line comes back and a second accept duplicates the fact.
    """

    note = "The harbour checklist was already saved as a fact."
    for env_name in (
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        AGENT_API_KEY_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(store, note=note, suffix="saved", minute=4)
    source_id = str(source["id"])
    run_local_vault_sleep(database, user_id=USER_ID)
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    commit_line = next(line for line in listing.splitlines() if line.startswith("alice_memory_commit: "))
    arguments = json.loads(commit_line.split(": ", 1)[1])
    from alicebot_api.mcp.registry import call_mcp_tool

    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)
    payload = call_mcp_tool(context, name="alice_memory_commit", arguments=arguments)
    assert payload["status"] == "committed", payload
    again = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    offered = [
        line for line in again.splitlines() if line.startswith("alice_memory_commit: ") and source_id in line
    ]
    assert offered == []
    assert _line_value(again, "rows not shown") == "1"


def test_hidden_rows_are_counted_when_nothing_is_listed(tmp_path: Path) -> None:
    """Private rows can fill the cap while a public fence lists nothing.

    Mutation: return only 'No sleep proposals.' The count of rows not shown
    disappears, and the full cap looks empty.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(SLEEP_PROPOSAL_CAP):
            _create_source(
                store,
                note=f"Private harbour salary note {index}.",
                suffix=f"hid{index}",
                minute=index + 1,
                sensitivity="private",
            )
        _create_source(
            store,
            note="The public harbour checklist stays visible.",
            suffix="visible",
            minute=40,
            sensitivity="public",
        )
    first = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(first, "proposals written")) == SLEEP_PROPOSAL_CAP
    second = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(second, "proposals written")) == 0
    assert int(_line_value(second, "sources not proposed")) == 1
    listing = compile_sleep_proposal_listing(
        database,
        user_id=USER_ID,
        effective_domains=(),
        effective_sensitivity_allowed=("public", "unknown"),
        effective_project_scope=(),
    )
    assert listing.splitlines()[0] == NO_SLEEP_PROPOSALS
    assert _line_value(listing, "rows not shown") == str(SLEEP_PROPOSAL_CAP)
    assert "Private harbour salary" not in listing
    assert "public harbour checklist" not in listing


def test_commit_line_escapes_a_line_separator(tmp_path: Path) -> None:
    """U+2028 in an excerpt stays inside the commit line.

    Mutation: dump the commit arguments with ensure_ascii false. The line
    separator splits the arguments and they no longer parse as one object.
    """

    database = _database(tmp_path)
    note = "The harbour radio stays on channel 7."
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(store, note=note, suffix="line", minute=2)
    source_id = str(source["id"])
    excerpt = "Keep the harbour radio" + "\u2028" + "on one commit line."
    _write_jsonl(
        sleep_proposals_path(database),
        [
            {
                "excerpt": excerpt,
                "source_id": source_id,
                "status": "proposed",
                "user_id": USER_ID,
            }
        ],
    )
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    commit_lines = [line for line in listing.splitlines() if line.startswith("alice_memory_commit: ")]
    assert len(commit_lines) == 1
    parsed = json.loads(commit_lines[0].split(": ", 1)[1])
    assert parsed["canonical_text"] == excerpt
    assert "\u2028" not in commit_lines[0]
    assert "\\u2028" in commit_lines[0]


def test_listing_rechecks_the_stored_excerpt(tmp_path: Path) -> None:
    """A credential that is only in the stored excerpt is left out.

    The source chunk is an ordinary note. Mutation: re-check the chunk and
    not the excerpt. The row is offered.
    """

    ordinary = "The public harbour checklist stays on channel 7."
    planted = f"we decided {_legacy_assignment()} for the billing store"
    assert credential_verdict(planted) is None
    assert commit_gate_refuses("", planted) is True
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(store, note=ordinary, suffix="excerpt", minute=1)
    source_id = str(source["id"])
    _write_jsonl(
        sleep_proposals_path(database),
        [
            {
                "excerpt": planted,
                "source_id": source_id,
                "status": "proposed",
                "user_id": USER_ID,
            }
        ],
    )
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    assert listing.splitlines()[0] == NO_SLEEP_PROPOSALS
    assert _legacy_assignment() not in listing
    assert source_id not in listing
    assert _line_value(listing, "rows not shown") == "1"


def test_listing_rechecks_the_first_chunk_window(tmp_path: Path) -> None:
    """A credential that is only in the first chunk window is left out.

    The stored excerpt is an ordinary sentence. Mutation: re-check the
    excerpt and not the chunk window. The row is offered.
    """

    ordinary = "The public harbour checklist stays on channel 7."
    planted = f"rotate {_floor_token()} before the deploy"
    assert credential_verdict(planted) is not None
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(store, note=planted, suffix="chunk", minute=1)
    source_id = str(source["id"])
    _write_jsonl(
        sleep_proposals_path(database),
        [
            {
                "excerpt": ordinary,
                "source_id": source_id,
                "status": "proposed",
                "user_id": USER_ID,
            }
        ],
    )
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    assert listing.splitlines()[0] == NO_SLEEP_PROPOSALS
    assert _floor_token() not in listing
    assert ordinary not in listing
    assert _line_value(listing, "rows not shown") == "1"


def test_listing_keeps_a_clean_excerpt_when_the_later_token_trips_the_gate(tmp_path: Path) -> None:
    """A kept row whose chunk fails only past the cut is still listed.

    Mutation: re-check the whole first chunk. The proposal disappears.
    """

    head = "n" * SLEEP_EXCERPT_MAX
    note = head + " The password policy is 12-characters for every harbour account."
    assert commit_door_secret_verdict("", head) is None
    assert commit_door_secret_verdict("", note) is not None
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source = _create_source(store, note=note, suffix="pastcut", minute=1)
    source_id = str(source["id"])
    report = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(report, "proposals written")) == 1
    assert int(_line_value(report, "sources withheld")) == 0
    listing = compile_sleep_proposal_listing(database, user_id=USER_ID, **OPEN_FENCE)
    assert f"source_id: {source_id}" in listing
    assert "12-characters" not in listing
    assert "rows not shown:" not in listing


def test_receipt_always_prints_six_lines(tmp_path: Path) -> None:
    """Zero withheld and zero removed are still printed.

    Mutation: print those two lines only when the count is positive.
    """

    database = _database(tmp_path)
    _seed_unlinked(database, 1, user_id=USER_ID, suffix="six")
    report = run_local_vault_sleep(database, user_id=USER_ID)
    for label in (
        "proposals written",
        "already present",
        "skipped as already linked",
        "cap",
        "sources withheld",
        "existing rows removed",
    ):
        assert any(line.startswith(f"{label}: ") for line in report.splitlines())
    assert _line_value(report, "sources withheld") == "0"
    assert _line_value(report, "existing rows removed") == "0"
    assert _line_value(report, "proposals written") == "1"


def test_a_refused_sidecar_row_does_not_fill_a_cap_slot(tmp_path: Path) -> None:
    """The cap counts kept rows, not a row the credential check drops.

    Mutation: count every existing sidecar row toward the cap. Eight clean
    sources then write only seven.
    """

    planted = f"we decided {_legacy_assignment()} for the billing store"
    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        refused = _create_source(store, note=planted, suffix="refused", minute=1)
        for index in range(SLEEP_PROPOSAL_CAP):
            _create_source(
                store,
                note=f"Unlinked harbour clipboard note cap {index}.",
                suffix=f"cap{index}",
                minute=index + 2,
            )
    _write_jsonl(
        sleep_proposals_path(database),
        [
            {
                "excerpt": planted,
                "source_id": str(refused["id"]),
                "status": "proposed",
                "user_id": USER_ID,
            }
        ],
    )
    report = run_local_vault_sleep(database, user_id=USER_ID)
    assert int(_line_value(report, "existing rows removed")) == 1
    assert int(_line_value(report, "proposals written")) == SLEEP_PROPOSAL_CAP
    assert "sources not proposed:" not in report
    assert _legacy_assignment() not in sleep_proposals_path(database).read_text(encoding="utf-8")


def test_doctor_reports_an_unreadable_sidecar_and_keeps_the_census(tmp_path: Path) -> None:
    """An unreadable sidecar does not drop the other doctor lines.

    Mutation: let the read error leave compile_local_vault_doctor. The
    census never prints.
    """

    database = _database(tmp_path)
    _seed_unlinked(database, 1, user_id=USER_ID, suffix="doc")
    sidecar = sleep_proposals_path(database)
    sidecar.mkdir()
    report = compile_local_vault_doctor(database, user_id=USER_ID)
    assert _line_value(report, "sleep proposals") == "unreadable"
    assert _line_value(report, "sources") == "1"
    assert _line_value(report, "searchable chunks") == "1"
    assert _line_value(report, "committed facts") == "0"
    assert _line_value(report, "candidates waiting") == "0"
    assert "last brief:" in report


def test_doctor_still_fails_when_the_sidecar_is_invalid(tmp_path: Path) -> None:
    """A corrupt sidecar is not reported as unreadable.

    Mutation: catch every sleep error and print unreadable. This test fails.
    """

    database = _database(tmp_path)
    sleep_proposals_path(database).write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(SleepError, match="invalid"):
        compile_local_vault_doctor(database, user_id=USER_ID)


def test_listing_calls_the_shared_commit_door_helper() -> None:
    """One first-chunk helper, and the listing uses the shared door.

    Mutation: copy a second _first_chunk_text, or check the listing with
    credential_verdict directly. This test fails.
    """

    import alicebot_api.vault_sleep as sleep

    source = inspect.getsource(sleep)
    assert source.count("def _first_chunk_text(") == 1
    listing = inspect.getsource(sleep.compile_sleep_proposal_listing)
    assert "_text_refused" in listing
    assert "credential_verdict" not in listing
    assert "commit_gate_refuses" not in listing
    assert "commit_door_secret_verdict" in inspect.getsource(sleep._text_refused)


def _memory_count(database: Path) -> int:
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return len(store.list_memories())


def test_write_jsonl_symbol_stays_the_writer() -> None:
    """The listing test patches this name. Keep it on the module."""

    assert callable(_write_jsonl)
