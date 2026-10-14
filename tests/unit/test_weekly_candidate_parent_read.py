"""The read and the stored-row classifier judge a weekly candidate the same way.

v0.20.0 wrote the inputs on the weekly report and named the memory in ``candidate_memory_ids``.
The read used to see only the memory, call it a row with no record, and hide it from a default
recall. The classifier already read the report. These tests require the two verdicts to match
for every row of a mixed vault, including that memory.

SQLite has no generated-artifact table, and a PostgreSQL dump is not an import path into it, so
a weekly candidate cannot arrive there with its report. Both verdicts then say the memory has no
record.

Mutations: stop loading the report that names the memory; compare only some row kinds.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_derived_labels import identifier, settle_labels, weekly_candidate_memory, with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_repair import classify_stored_labels, format_label_check
from alicebot_api.vnext_label_repair import _load_tables
from alicebot_api.vnext_label_writes import without_insert_floor

COVERED_ROW_KINDS = ("weekly", "consolidation", "copy", "report_of_report")
DEFAULT_SENSITIVITY = ("public", "internal", "private", "unknown")
USER = "user-1"


def _row(kind: str, row_id: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": kind,
        "id": row_id,
        "user_id": USER,
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": {},
    }
    row.update(fields)
    return row


def _summary(source_ids: list[str]) -> dict[str, object]:
    return {"source_ids": source_ids, "counts": {"sources": len(source_ids)}}


class Vault:
    """Rows in memory, with the two reads the guard uses."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.parent_reads = 0

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        wanted = {identifier(item) for item in ids}
        return [
            dict(row)
            for row in self.rows
            if row["kind"] == kind and identifier(row["id"]) in wanted
        ]

    def read_artifacts_naming_candidates(self, memory_ids: list[str]) -> list[dict[str, object]]:
        self.parent_reads += 1
        wanted = {identifier(item) for item in memory_ids}
        found = []
        for row in self.rows:
            if row["kind"] != "artifact":
                continue
            meta = row["metadata_json"]
            named = meta.get("candidate_memory_ids") if isinstance(meta, dict) else None
            if isinstance(named, list) and any(identifier(item) in wanted for item in named):
                found.append(dict(row))
        return found


def mixed_vault() -> tuple[Vault, dict[str, str]]:
    """One of each kind the agreement has to cover, plus a weekly memory no report names."""

    public = _row("source", "source-public", sensitivity="public")
    secret = _row("source", "source-secret", sensitivity="confidential")
    member = _row("memory", "member")
    original = _row("memory", "original")
    consolidated = _row(
        "memory",
        "consolidated",
        metadata_json={
            "candidate_kind": "memory_consolidation",
            "consolidation": {"cluster_member_ids": ["member"]},
        },
    )
    copied = _row("memory", "copied", metadata_json={"source_id": "source-public"})
    weekly = _row(
        "memory",
        "weekly",
        metadata_json={"candidate": True, "discovered_by": "vnext_weekly_synthesis", "project_scope": []},
    )
    weekly_secret = _row(
        "memory",
        "weekly-secret",
        sensitivity="public",
        metadata_json={"candidate": True, "discovered_by": "vnext_weekly_synthesis", "project_scope": []},
    )
    weekly_alone = _row(
        "memory",
        "weekly-alone",
        metadata_json={"candidate": True, "discovered_by": "vnext_weekly_synthesis", "project_scope": []},
    )
    recorded = _row(
        "memory",
        "weekly-recorded",
        metadata_json=with_derived_from(
            {"candidate": True, "discovered_by": "vnext_weekly_synthesis", "project_scope": []},
            {"sources": [public]},
        ),
    )
    inner = _row(
        "artifact",
        "inner-report",
        artifact_type="daily_brief",
        metadata_json=with_derived_from(
            {"workflow": "daily_brief", "input_summary": _summary(["source-public"])},
            {"sources": [public]},
        ),
    )
    outer = _row(
        "artifact",
        "outer-report",
        artifact_type="daily_brief",
        metadata_json=with_derived_from(
            {"workflow": "daily_brief", "input_summary": _summary(["source-public"])},
            {"artifacts": [inner], "sources": [public]},
        ),
    )
    weekly_report = _row(
        "artifact",
        "weekly-report",
        artifact_type="weekly_synthesis",
        metadata_json={
            "workflow": "weekly_synthesis",
            "candidate_memory_ids": ["weekly"],
            "input_summary": _summary(["source-public"]),
        },
    )
    secret_report = _row(
        "artifact",
        "secret-report",
        artifact_type="weekly_synthesis",
        metadata_json={
            "workflow": "weekly_synthesis",
            "candidate_memory_ids": ["weekly-secret"],
            "input_summary": _summary(["source-secret"]),
        },
    )
    recorded_report = _row(
        "artifact",
        "recorded-report",
        artifact_type="weekly_synthesis",
        metadata_json={
            "workflow": "weekly_synthesis",
            "candidate_memory_ids": ["weekly-recorded"],
            "input_summary": _summary(["source-public"]),
        },
    )
    rows = [
        public, secret, member, original, consolidated, copied, weekly, weekly_secret, weekly_alone,
        recorded, inner, outer, weekly_report, secret_report, recorded_report,
    ]
    kinds = {
        "weekly": "weekly",
        "consolidation": "consolidated",
        "copy": "copied",
        "report_of_report": "outer-report",
    }
    return Vault(rows), kinds


def _classifier(vault: Vault) -> dict[str, tuple[str, str, bool]]:
    settled = settle_labels(vault.rows, on_cycle="unverified")
    found = {}
    for label in settled.rows:
        if label.unverified:
            found[label.stored_id] = (label.domain, "regulated", True)
        else:
            found[label.stored_id] = (label.domain, label.sensitivity, False)
    return found


def _read(vault: Vault) -> dict[str, tuple[str, str, bool]]:
    found = {}
    with label_read_scope(vault):
        guard = LabelGuard.for_filters(vault, (), DEFAULT_SENSITIVITY, ())
        for row in vault.rows:
            effective = guard.effective_row(str(row["kind"]), row)
            assert isinstance(effective, dict)
            found[str(row["id"])] = (
                str(effective.get("domain")),
                str(effective.get("sensitivity")),
                bool(effective.get("unverified")),
            )
    return found


def test_read_and_classifier_agree_for_every_row() -> None:
    """Every row, not a sample of kinds. A weekly memory takes its report's inputs."""

    assert COVERED_ROW_KINDS == ("weekly", "consolidation", "copy", "report_of_report")
    vault, kinds = mixed_vault()
    classifier = _classifier(vault)
    read = _read(vault)
    assert set(read) == set(classifier)
    assert read == classifier
    compared = {name for name, row_id in kinds.items() if read[row_id] == classifier[row_id]}
    assert set(COVERED_ROW_KINDS) <= compared
    assert read["weekly"] == ("project", "public", False)
    assert read["weekly-secret"] == ("project", "confidential", False)
    assert read["weekly-alone"][2] is True
    assert read["weekly-recorded"] == ("project", "public", False)
    assert read["original"] == ("project", "public", False)


def test_a_key_with_limits_cannot_read_a_candidate_whose_parent_inputs_are_confidential() -> None:
    vault, _kinds = mixed_vault()
    secret = next(row for row in vault.rows if row["id"] == "weekly-secret")
    public = next(row for row in vault.rows if row["id"] == "weekly")
    with label_read_scope(vault):
        guard = LabelGuard.for_filters(vault, (), DEFAULT_SENSITIVITY, ())
        assert guard.admit_rows("memory", [secret]) == []
        assert [row["id"] for row in guard.admit_rows("memory", [public])] == ["weekly"]
        bound = LabelGuard.for_filters(vault, (), DEFAULT_SENSITIVITY, ("alpha",), all_of=("alpha",))
        assert bound.admit_rows("memory", [secret]) == []


def test_a_memory_that_is_not_a_weekly_candidate_does_not_look_up_a_parent() -> None:
    vault, _kinds = mixed_vault()
    original = next(row for row in vault.rows if row["id"] == "original")
    with label_read_scope(vault):
        guard = LabelGuard.for_filters(vault, (), DEFAULT_SENSITIVITY, ())
        assert guard.admit_rows("memory", [original])
    assert vault.parent_reads == 0
    weekly = next(row for row in vault.rows if row["id"] == "weekly")
    with label_read_scope(vault):
        guard = LabelGuard.for_filters(vault, (), DEFAULT_SENSITIVITY, ())
        guard.effective_row("memory", weekly)
    assert vault.parent_reads == 1


def test_sqlite_has_no_report_table_so_an_unparented_weekly_candidate_stays_unverified(tmp_path) -> None:
    """A restored PostgreSQL export cannot put the report on SQLite. The check then lists the memory."""

    user_id = uuid4()
    path = tmp_path / "vault.db"
    token = "sqliteweeklytoken"
    with sqlite_user_connection(path, user_id, repair_labels=False) as conn:
        ensure_sqlite_user(conn, user_id, "owner@example.invalid", "Owner")
        store = SQLiteVNextStore(conn, user_id)
        assert store.read_artifacts_naming_candidates(["missing"]) == []
        with without_insert_floor():
            candidate = store.create_memory(
                {
                    "memory_type": "artifact_summary",
                    "memory_key": "weekly_synthesis." + uuid4().hex,
                    "value": {"insight": token},
                    "status": "active",
                    "confidence": 0.6,
                    "canonical_text": f"The weekly pattern names {token}.",
                    "summary": token,
                    "title": "Weekly candidate",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {
                        "candidate": True,
                        "discovered_by": "vnext_weekly_synthesis",
                        "project_scope": [],
                    },
                },
                actor_type="user",
            )
        assert weekly_candidate_memory("memory", candidate)
    with sqlite_user_connection(path, user_id) as conn:
        below, unverified = classify_stored_labels(_load_tables(conn))
    text = format_label_check(below, unverified)
    assert text.startswith("below_inputs 0")
    assert "no_record 1" in text
    assert str(candidate["id"]) in text
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    context = MCPRuntimeContext(database_url=f"sqlite:///{path}", user_id=user_id)
    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": token, "limit": 5})
    assert recall["count"] == 0
    every = ("public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown")
    named = call_mcp_tool(
        context,
        name="alice_recall",
        arguments={"query": token, "limit": 5, "sensitivity_allowed": list(every)},
    )
    assert named["count"] == 1


def test_sqlite_read_uses_a_parent_the_store_returns(tmp_path, monkeypatch) -> None:
    """The local store returns no report. The shared read still judges one a caller can supply."""

    user_id = uuid4()
    path = tmp_path / "vault.db"
    token = "suppliedparenttoken"
    with sqlite_user_connection(path, user_id, repair_labels=False) as conn:
        ensure_sqlite_user(conn, user_id, "owner@example.invalid", "Owner")
        store = SQLiteVNextStore(conn, user_id)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Public note",
                "content_hash": uuid4().hex,
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"raw_text": "public"},
            }
        )
        with without_insert_floor():
            candidate = store.create_memory(
                {
                    "memory_type": "artifact_summary",
                    "memory_key": "weekly_synthesis." + uuid4().hex,
                    "value": {"insight": token},
                    "status": "active",
                    "canonical_text": f"The weekly pattern names {token}.",
                    "summary": token,
                    "title": "Weekly candidate",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {
                        "candidate": True,
                        "discovered_by": "vnext_weekly_synthesis",
                        "project_scope": [],
                    },
                },
                actor_type="user",
            )
    parent = {
        "id": str(uuid4()),
        "user_id": str(user_id),
        "domain": "project",
        "sensitivity": "public",
        "artifact_type": "weekly_synthesis",
        "metadata_json": {
            "workflow": "weekly_synthesis",
            "candidate_memory_ids": [str(candidate["id"])],
            "input_summary": _summary([str(source["id"])]),
            "source_refs": [f"source:{source['id']}"],
        },
    }

    def supply(self, memory_ids):
        del self
        return [parent] if str(candidate["id"]) in {str(item) for item in memory_ids} else []

    monkeypatch.setattr(SQLiteVNextStore, "read_artifacts_naming_candidates", supply)
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    context = MCPRuntimeContext(database_url=f"sqlite:///{path}", user_id=user_id)
    recall = call_mcp_tool(context, name="alice_recall", arguments={"query": token, "limit": 5})
    assert [row["id"] for row in recall["results"]] == [str(candidate["id"])]
