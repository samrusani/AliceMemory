"""On SQLite a card, candidate or copy made from a memory that is redacted afterwards is unverified until it is regenerated.

The same rule as on PostgreSQL, through the doors SQLite has and a key can call: recall, the context pack, resume, recent
decisions, the review queue and its detail, explain, the open-loop list, the doctor and ``labels check``. The graph routes are
PostgreSQL only; the guard that judges a graph edge is run here on the SQLite store. A real key of each
profile reads a vault where a roll-up card, a weekly candidate, a project-update candidate, a consolidation candidate and a copy
of a copy hold the words of a memory that holds a sentinel, and the memory is redacted through the flow the redact verb uses.

Mutations that these tests must fail (the manifest replays each one): the kernel stops reading a redacted input; the guard
settles a row from a redacted parent; the reduced rank proof stops counting a row with a redacted input at the top rank.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError, MCPToolNotFoundError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_repair import REDACTED_INPUT_ADVICE, label_gap_report
from alicebot_api.vnext_rollups import VNextRollupService
from alicebot_api.vault_doctor import compile_local_vault_doctor

ALPHA = "prj_" + "a" * 16
PROFILES = {
    "admin": ("admin_agent", None),
    "trusted": ("trusted_local_agent", None),
    "read_only": ("read_only_agent", None),
    "memory_proposal": ("memory_proposal_agent", None),
    "trusted_bound": ("trusted_local_agent", ALPHA),
    "alpha_only": ("project_scoped_agent", ALPHA),
    "admin_bound": ("admin_agent", ALPHA),
}
RESTRICTED = tuple(name for name in PROFILES if name != "admin")
GAMES = ("Hollow Knight", "Stardew Valley", "Celeste", "Hades", "Tunic")


class Vault:
    def __init__(self, tmp_path, monkeypatch):
        self.user = uuid4()
        self.path = tmp_path / "redacted.sqlite3"
        self.sentinel = f"ZQXSENTINEL{uuid4().hex[:10]}"
        bootstrap_database(self.path, user_id=str(self.user), user_email="synthetic@example.invalid")
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        self.monkeypatch = monkeypatch
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            self.source = store.create_source(
                {"source_type": "note", "title": "SOURCE Atlas", "content_hash": str(uuid4()), "domain": "project",
                 "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}}
            )
            self.members = []
            for index, game in enumerate(GAMES):
                label = self.sentinel if index == 3 else game
                text = f"Atlas played {label} for {25 + index * 30} hours. MEMORY{index}"
                self.members.append(store.create_memory(
                    {"memory_key": f"alpha.game.{index}", "memory_type": "episode", "title": text, "canonical_text": text, "summary": text,
                     "value": {"text": text}, "status": "active", "domain": "project", "sensitivity": "public",
                     "metadata_json": {"project_scope": [ALPHA], "session_date": f"2023-06-0{index + 1}"}}
                ))
            self.redacted = str(self.members[3]["id"])
            run = VNextRollupService(store).propose_rollups(projects=(ALPHA,))
            self.old_cards = list(run.candidate_ids)
            assert self.old_cards
            sentinel_text = f"Atlas played {self.sentinel} for 115 hours."
            used = {"memories": [self.members[3], self.members[0]]}
            self.weekly = self._derived(store, "weekly", {"discovered_by": "vnext_weekly_synthesis"}, used, sentinel_text)
            self.update = self._derived(store, "update", {"workflow": "project_auto_update"}, used, f"Project state: {sentinel_text}")
            self.consolidation = self._derived(
                store, "consolidation",
                {"consolidation": {"cluster_member_ids": [str(self.members[3]["id"]), str(self.members[0]["id"])],
                                   "member_snapshots": [{"id": str(self.members[3]["id"]), "status": "active", "content_digest": "0123456789abcdef"}]}},
                used, f"Merge: {sentinel_text}",
            )
            # A copy of a copy: it names the weekly candidate, never the redacted memory.
            self.chain = self._derived(store, "chain", {"discovered_by": "vnext_weekly_synthesis"}, {"memories": [self.weekly]}, f"Again: {sentinel_text}")
            self.clear = store.create_memory(
                {"memory_key": "alpha.clear", "memory_type": "episode", "title": "Atlas clear note", "canonical_text": "Atlas clear note MEMORY9",
                 "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": [ALPHA]}}
            )
            self.keys = {
                name: create_agent_key(store, user_id=self.user, agent_id=name, permission_profile=profile, project_scope=bound)[1]
                for name, (profile, bound) in PROFILES.items()
            }
            self.contained = [str(row["id"]) for row in (self.weekly, self.update, self.consolidation, self.chain)] + [
                card for card in self.old_cards
            ]

    def _derived(self, store, name, markers, used, text):
        metadata = with_derived_from({**markers, "project_scope": [ALPHA]}, used)
        return store.create_memory(
            {"memory_key": f"derived.{name}", "memory_type": "episode", "title": text, "canonical_text": text, "summary": text,
             "value": {"text": text}, "status": "candidate" if name != "chain" else "active", "domain": "project", "sensitivity": "public",
             "metadata_json": metadata}
        )

    def redact(self):
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            return redact_memory_flow(store, memory_id=self.redacted, reason="synthetic")

    def call(self, key, tool, arguments):
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=UUID(str(self.user)))
        return call_mcp_tool(context, name=tool, arguments=arguments)

    def sweep(self, key):
        """Every answer the SQLite doors give this key."""
        answers = []
        queries = ("Atlas played hours", "Atlas clear note", "Project state", "Merge Atlas", "Again")
        calls = []
        for query in queries:
            calls += [("alice_recall", {"query": query, "limit": 50}), ("alice_context_pack", {"query": query})]
        calls += [
            ("alice_resume", {}), ("alice_recent_decisions", {}), ("alice_open_loops", {"action": "list"}),
            ("alice_memory_review", {"status": "all", "limit": 100}),
        ]
        with sqlite_user_connection(self.path, self.user) as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM memories").fetchall()]
        for memory_id in ids:
            calls += [("alice_explain", {"memory_id": memory_id}), ("alice_memory_review", {"review_item_id": memory_id})]
        for tool, arguments in calls:
            try:
                answers.append((tool, arguments, self.call(key, tool, arguments)))
            except (MCPToolError, MCPToolNotFoundError) as exc:
                answers.append((tool, arguments, {"error": str(exc)}))
        return answers


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return Vault(tmp_path, monkeypatch)


def _ids(body, found=None):
    found = set() if found is None else found
    if isinstance(body, dict):
        for key, child in body.items():
            if key == "id" and isinstance(child, str):
                found.add(child)
            _ids(child, found)
    elif isinstance(body, list):
        for child in body:
            _ids(child, found)
    return found


def test_the_vault_holds_the_words_before_the_memory_is_redacted_and_in_the_stored_rows_after(vault):
    """The control: before redaction a restricted key reads the roll-up card and the candidates, with the sentinel."""
    seen = vault.sweep(vault.keys["trusted"])
    assert vault.sentinel in json.dumps(seen, default=str)
    result = vault.redact()
    assert result["status"] == "redacted"
    with sqlite_user_connection(vault.path, vault.user) as conn:
        rows = {row["id"]: json.dumps(dict(row), default=str) for row in conn.execute("SELECT * FROM memories").fetchall()}
    assert vault.sentinel not in rows[vault.redacted]
    assert all(vault.sentinel in rows[item] for item in vault.contained), "containment is not removal: the rows keep the words"


@pytest.mark.parametrize("profile", RESTRICTED)
def test_no_restricted_profile_recovers_the_text_of_a_redacted_memory_through_any_sqlite_door(vault, profile):
    vault.redact()
    answers = vault.sweep(vault.keys[profile])
    assert any("error" not in body for _tool, _arguments, body in answers), "the profile reads something"
    leaked = [(tool, arguments) for tool, arguments, body in answers if vault.sentinel in json.dumps(body, default=str)]
    assert not leaked, leaked[:5]
    shown = set().union(*(_ids(body) for _tool, _arguments, body in answers))
    assert not shown & set(vault.contained)
    # A row that recorded nothing redacted is still read, wherever the profile reads memories at all.
    readable = [body for tool, arguments, body in answers if tool == "alice_explain" and arguments["memory_id"] == str(vault.clear["id"])]
    assert readable and "error" not in readable[0] or profile in {"memory_proposal", "alpha_only", "read_only"}


def test_the_owner_and_an_unbound_admin_key_still_read_the_rows_with_their_words(vault):
    vault.redact()
    for key in (None, vault.keys["admin"]):
        for row_id in vault.contained:
            body = vault.call(key, "alice_explain", {"memory_id": row_id})
            assert vault.sentinel in json.dumps(body, default=str), (key is None, row_id)
        detail = vault.call(key, "alice_memory_review", {"review_item_id": vault.contained[0]})
        assert vault.sentinel in json.dumps(detail, default=str), (key is None, "review detail")


def test_a_row_built_from_a_row_that_read_the_memory_is_contained_too(vault):
    """The copy of a copy never named the redacted memory. It is restricted because the weekly candidate it names is."""
    chain = str(vault.chain["id"])
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert vault.redacted not in json.dumps(dict(conn.execute("SELECT * FROM memories WHERE id = ?", (chain,)).fetchone()), default=str)
    vault.redact()
    for profile in RESTRICTED:
        with pytest.raises(MCPToolError):
            vault.call(vault.keys[profile], "alice_explain", {"memory_id": chain})
    assert vault.sentinel in json.dumps(vault.call(vault.keys["admin"], "alice_explain", {"memory_id": chain}), default=str)


def test_regenerating_the_roll_up_restores_access_only_without_the_redacted_text(vault):
    vault.redact()
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        run = VNextRollupService(store).propose_rollups(projects=(ALPHA,))
        new_cards = [card for card in run.candidate_ids if card not in vault.old_cards]
        stored = {card: json.dumps(dict(store.get_memory(card)), default=str) for card in new_cards}
    assert new_cards, "the run made a new card"
    for card, body in stored.items():
        assert vault.sentinel not in body
        assert vault.redacted not in body
    for profile in ("trusted", "trusted_bound"):
        for card in new_cards:
            assert vault.call(vault.keys[profile], "alice_explain", {"memory_id": card})["memory"]["id"] == card
        for card in vault.old_cards:
            with pytest.raises(MCPToolError):
                vault.call(vault.keys[profile], "alice_explain", {"memory_id": card})
    for card in vault.old_cards:
        assert vault.sentinel in json.dumps(vault.call(vault.keys["admin"], "alice_explain", {"memory_id": card}), default=str)


def test_archiving_a_memory_does_not_restrict_the_rows_built_from_it(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        store.update_memory(memory_id=vault.redacted, patch={"status": "archived"}, actor_type="system")
    for row_id in vault.contained:
        body = vault.call(vault.keys["trusted"], "alice_explain", {"memory_id": row_id})
        assert vault.sentinel in json.dumps(body, default=str)
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert label_gap_report(SQLiteVNextStore(conn, vault.user)).unverified == 0


def test_redaction_clears_a_cached_label_inside_one_request_scope_on_sqlite(vault):
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        weekly = store.get_memory(str(vault.weekly["id"]))
        with label_read_scope(store):
            guard = LabelGuard(store, active=True, sensitivity_allowed=("public", "internal", "unknown", "private"))
            assert guard.effective_row("memory", weekly)["unverified"] is False
            assert [row["id"] for row in guard.admit_rows("memory", [weekly])] == [weekly["id"]]
            before = guard.readable_status_counts("memory")
            store.redact_memory_bundle(memory_id=vault.redacted, project_update_artifacts=[], actor_type="user")
            assert guard.effective_row("memory", weekly)["unverified"] is True
            assert guard.admit_rows("memory", [weekly]) == []
            after = guard.readable_status_counts("memory")
    assert sum(after.values()) < sum(before.values())


def test_the_sqlite_doctor_and_labels_check_say_to_regenerate_or_delete(vault, capsys):
    from alicebot_api.label_commands import run_labels

    vault.redact()
    with sqlite_user_connection(vault.path, vault.user) as conn:
        gap = label_gap_report(SQLiteVNextStore(conn, vault.user))
    assert gap.below == 0 and gap.redacted_inputs >= 4 and gap.unverified >= gap.redacted_inputs + 1
    text = compile_local_vault_doctor(vault.path, user_id=vault.user)
    line = next(item for item in text.splitlines() if item.startswith("derived labels:"))
    assert f"{gap.unverified} unverified ({gap.redacted_inputs} built from a redacted memory)" in line
    assert REDACTED_INPUT_ADVICE in line
    args = SimpleNamespace(data_dir=None, db=str(vault.path), user_id=str(vault.user), labels_command="check")
    assert run_labels(args) == 1
    out = capsys.readouterr().out
    assert f"input_redacted {gap.redacted_inputs}" in out and "dependency_unverified" in out
    assert REDACTED_INPUT_ADVICE in out
    # Repair cannot fix them: it changes nothing and the count stays.
    repair = SimpleNamespace(data_dir=None, db=str(vault.path), user_id=str(vault.user), labels_command="repair")
    assert run_labels(repair) == 0
    assert "labels repair updated 0" in capsys.readouterr().out
    with sqlite_user_connection(vault.path, vault.user) as conn:
        assert label_gap_report(SQLiteVNextStore(conn, vault.user)) == gap


def test_a_guard_admits_a_graph_edge_only_when_every_labelled_end_is_readable_and_not_redacted(vault):
    """An edge has no label and keeps the explanation it was made with. The guard reads each end of it, on the SQLite store too."""
    vault.redact()
    source_id = str(vault.source["id"])
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        confidential = store.create_memory(
            {"memory_key": "alpha.confidential", "memory_type": "episode", "title": "CONFSECRET", "canonical_text": "CONFSECRET",
             "status": "active", "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}}
        )

        def edge(name, to_type, to_id):
            row = store.create_graph_edge(
                {"from_type": "source", "from_id": source_id, "to_type": to_type, "to_id": str(to_id), "edge_type": "mentions",
                 "confidence": 0.5, "explanation": name, "created_by": "test", "metadata_json": {"status": "candidate"}}
            )
            return str(row["id"])

        made = {
            "clear": edge("clear", "memory", vault.clear["id"]),
            "redacted": edge("redacted", "memory", vault.redacted),
            "above the ceiling": edge("above the ceiling", "memory", confidential["id"]),
            "contained copy": edge("contained copy", "memory", vault.chain["id"]),
            "missing": edge("missing", "memory", uuid4()),
            "entity": edge("entity", "entity", uuid4()),
        }
        stored = store.list_edges(from_id=source_id)
        assert {row["id"] for row in stored} == set(made.values())
        limited = LabelGuard.for_filters(store, (), ("public", "internal", "private", "unknown"), ())
        assert {row["id"] for row in limited.admit_edges(stored)} == {made["clear"], made["entity"]}
        # A caller with no ceiling is not limited, so every edge stays, the redacted memory's too.
        open_guard = LabelGuard.for_filters(store, (), ALL_SENSITIVITY, ())
        assert {row["id"] for row in open_guard.admit_edges(stored)} == set(made.values())
