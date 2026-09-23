"""The credential floor holds on every door the 2026-09-03 dump opened.

Why this file exists. On 2026-09-22 (ticket S4.4) every one of these was
reproduced against the shipped code, asserting at the store: a credential
split across title and body committed active (door 2); correct() and
confirm(action=edit) rewrote a row's text to a GitHub token the commit gate
refuses (door 3); artifact promotion made the token an active,
human_curated, confidence 1.0 memory (door 5); alice-memory import restored a
crafted export with the token in an active row (door 6); and the assignment
regex took 43 s on 50 KB of "a_" (door 7). Door 4, the /v1 memory operations,
is Postgres only and lives in tests/integration.

How they escaped. The existing floor tests assert at evaluate_promotion and
hard_floor_hits, which is the promotion layer. None of these paths calls it,
so the suite stayed green while the writes landed. Every test here asserts
at the store: the row that must not exist, or the row that must not change.

Door 1, instruction-shaped content committing directly, is deliberately not
here. It is an owner decision and this change does not touch it.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import time
from uuid import uuid4

import pytest

from alicebot_api import credential_floor, vnext_promotion_policy as policy
from alicebot_api.continuity_objects import ContinuityObjectValidationError, create_continuity_object_record
from alicebot_api.continuity_review import ContinuityReviewValidationError, apply_continuity_correction
from alicebot_api.contracts import ContinuityCorrectionInput, MemoryOperationGenerateInput
from alicebot_api.credential_floor import (
    CREDENTIAL_MATERIAL_REFUSED_MESSAGE,
    carries_credential_material,
    refuse_credential_material,
)
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool
from alicebot_api.memory_mutations import MemoryMutationValidationError, generate_memory_operation_candidates
from alicebot_api.onramp import bootstrap_database, main as onramp_main, sqlite_url_for_path
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_memory_commit import (
    MAX_COMMIT_SOURCE_REF_CHARS,
    MAX_COMMIT_SOURCE_REFS,
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_queue import VNextQueueService, VNextQueueValidationError

from tests.unit.fixtures_promotion_corpus import ALL_NOTES, BUILDER_NOTES
from tests.unit.fixtures_timing import budget, tracer_active
from tests.unit.test_memory_mutations import MemoryMutationStoreStub


# Built rather than written out so the source carries no scanner-shaped token.
PAT = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
HEX64 = "3251d49217b3064ebffc565025e4e36f176ecd6003287505108d1a8ec2fb0d17"
USER_ID = "00000000-0000-4000-8000-0000000000aa"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_embedding_or_promotion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        policy.PROMOTION_PERSONA_ENV,
        policy.PROMOTION_FILTERS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "floor@example.com")
    return SQLiteVNextStore(conn, user_id)


def _cells(row: object, names: tuple[str, ...]) -> tuple[object, ...]:
    # A bare connection yields tuples; the product connection yields mappings.
    if isinstance(row, tuple):
        return row
    return tuple(row[name] for name in names)  # type: ignore[index]


def _rows(store: SQLiteVNextStore) -> list[tuple[str, ...]]:
    names = ("id", "status", "title", "canonical_text")
    return [
        tuple(str(cell) for cell in _cells(row, names))
        for row in store.conn.execute(f"SELECT {', '.join(names)} FROM memories ORDER BY created_at")
    ]


def _commit(store: SQLiteVNextStore, **payload: object) -> dict[str, object]:
    service = VNextMemoryCommitService(store)
    return service.commit(identity=None, request=memory_commit_request_from_payload(payload, user_id=store.user_id))


def _stored_text_anywhere(store: SQLiteVNextStore, needle: str) -> list[str]:
    """Every memory column that carries the needle, whatever the row status."""

    names = ("id", "title", "canonical_text", "summary", "value")
    hits: list[str] = []
    for row in store.conn.execute(f"SELECT {', '.join(names)} FROM memories"):
        cells = _cells(row, names)
        for column, value in zip(names[1:], cells[1:]):
            if needle in str(value or ""):
                hits.append(f"{cells[0]}.{column}")
    return hits


# ---------------------------------------------------------------------------
# The choke point itself.
# ---------------------------------------------------------------------------


def test_the_choke_point_joins_fields_in_reading_order() -> None:
    assert carries_credential_material("id AK", "IAIOSFODNN7EXAMPLE is the access key id")
    # Reading order is what reassembles the key: the other way round spells
    # nothing.
    assert not carries_credential_material("IAIOSFODNN7EXAMPLE is the access key id", "id AK")
    assert carries_credential_material({"notes": [{"k": f"use {PAT}"}]})
    assert not carries_credential_material(None, "", [], {})


def test_the_choke_point_raises_the_callers_own_error() -> None:
    class DoorError(ValueError):
        pass

    with pytest.raises(DoorError, match="credential material"):
        refuse_credential_material("title", f"use {PAT}", error=DoorError)
    refuse_credential_material("Deploys go out on Tuesdays.", error=DoorError)
    assert "credential material" in CREDENTIAL_MATERIAL_REFUSED_MESSAGE


@pytest.mark.parametrize(("title", "text"), BUILDER_NOTES)
def test_the_choke_point_leaves_the_builder_corpus_alone(title: str, text: str) -> None:
    """The joins must not invent credentials out of ordinary notes."""

    assert not carries_credential_material(title, text)


# ---------------------------------------------------------------------------
# Door 2: a credential split across two fields.
# ---------------------------------------------------------------------------

_SPLITS = (
    ("id AK", "IAIOSFODNN7EXAMPLE is the access key id"),
    ("-----BEGIN RSA PRIVATE", "KEY----- MIIEowIBAAKCAQEA"),
)


@pytest.mark.parametrize(("title", "body"), _SPLITS)
def test_door2_a_credential_split_across_title_and_body_never_reaches_the_store(title: str, body: str) -> None:
    store = _store()

    # Guards the guard: each half alone must look clean, or this would pass
    # on per-field scanning and prove nothing about the join.
    assert not carries_credential_material(title)
    assert not carries_credential_material(body)

    result = _commit(store, title=title, canonical_text=body)

    assert result["status"] == "rejected"
    assert "unsafe_secret_storage" in result["reasons"]
    assert _rows(store) == []


@pytest.mark.parametrize(
    "extra",
    [{"idempotency_key": PAT}, {"trace_id": PAT}, {"project_scope": ["alice", PAT]}],
    ids=["idempotency_key", "trace_id", "project_scope"],
)
def test_round2_commit_reads_the_persisted_identifiers(extra: dict[str, object]) -> None:
    """Design ruling, extended fields (2026-09-23): identifiers are stored and
    replayed, so a key planted in one is refused like a key in the body."""

    store = _store()
    result = _commit(store, title="deploy", canonical_text="Deploys go out on Tuesdays.", **extra)
    assert result["status"] == "rejected"
    assert "unsafe_secret_storage" in result["reasons"]
    assert _rows(store) == []
    # Guards the guard: ordinary identifiers commit.
    clean = _commit(
        _store(), title="deploy", canonical_text="Deploys go out on Tuesdays.", idempotency_key=str(uuid4())
    )
    assert clean["status"] == "committed"


def test_door2_the_default_mcp_commit_tool_is_covered(tmp_path) -> None:
    context = _sqlite_context(tmp_path)
    result = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={"title": "id AK", "canonical_text": "IAIOSFODNN7EXAMPLE is the access key id"},
    )
    assert result["status"] == "rejected"
    with sqlite_user_connection(Path(tmp_path) / "memory.db", USER_ID) as conn:
        assert _rows(SQLiteVNextStore(conn, USER_ID)) == []


# ---------------------------------------------------------------------------
# Door 3: correct() and confirm(action=edit).
# ---------------------------------------------------------------------------


def test_door3_correct_refuses_a_credential_and_leaves_the_row_as_it_was() -> None:
    store = _store()
    # Guards the guard: the commit gate already refuses this text, so the
    # string is one the floor knows. The door was that correct() skipped it.
    assert _commit(store, title="token", canonical_text=f"use {PAT} for deploys")["status"] == "rejected"
    clean = _commit(store, title="deploy", canonical_text="Deploys go out on Tuesdays.")
    memory_id = str(clean["memory"]["id"])  # type: ignore[index]
    before = _rows(store)

    service = VNextMemoryCommitService(store)
    with pytest.raises(VNextMemoryCommitValidationError, match="credential material"):
        service.correct(identity=None, memory_id=memory_id, canonical_text=f"use {PAT} for deploys")

    assert _rows(store) == before
    assert _stored_text_anywhere(store, PAT) == []
    # And the path still works for ordinary text.
    service.correct(identity=None, memory_id=memory_id, canonical_text="Deploys go out on Wednesdays.")
    assert store.get_memory(memory_id)["canonical_text"] == "Deploys go out on Wednesdays."  # type: ignore[index]


@pytest.mark.parametrize("action", ["edit", "confirm"])
def test_door3_confirm_with_new_text_refuses_a_credential_and_leaves_the_row_pending(action: str) -> None:
    store = _store()
    pending = _commit(store, title="deploy day", canonical_text="Deploys may move to Wednesdays.", confidence=0.8)
    assert pending["status"] == "confirmation_required"
    memory_id = str(pending["memory"]["id"])  # type: ignore[index]
    before = _rows(store)
    assert before[0][1] == "needs_review"

    service = VNextMemoryCommitService(store)
    with pytest.raises(VNextMemoryCommitValidationError, match="credential material"):
        service.confirm(
            identity=None,
            confirmation_id=str(pending["confirmation_id"]),
            action=action,
            canonical_text=f"use {PAT} for deploys",
        )

    assert _rows(store) == before
    assert _stored_text_anywhere(store, PAT) == []
    # The confirmation is still pending and still confirmable as written.
    confirmed = service.confirm(identity=None, confirmation_id=str(pending["confirmation_id"]))
    assert confirmed["status"] == "committed"
    assert store.get_memory(memory_id)["canonical_text"] == "Deploys may move to Wednesdays."  # type: ignore[index]


def test_door3_confirm_reject_withholds_a_credential_rationale() -> None:
    """The rationale is stored in the row's metadata whichever way it resolves.

    Round 1 refused such a reject, which left the row pending. Owner ruling
    C6 (2026-09-23) changed that: a reject always completes, and the
    rationale is stored as a fixed placeholder. The full C6 set is in
    tests/unit/test_credential_activation_and_withhold.py.
    """

    store = _store()
    pending = _commit(store, title="deploy day", canonical_text="Deploys may move to Wednesdays.", confidence=0.8)
    service = VNextMemoryCommitService(store)
    rejected = service.confirm(
        identity=None,
        confirmation_id=str(pending["confirmation_id"]),
        action="reject",
        rationale=f"it leaked {PAT}",
    )
    assert (rejected["status"], rejected["rationale_withheld"]) == ("rejected", True)
    assert [row[1] for row in _rows(store)] == ["rejected"]
    assert PAT not in str(list(store.conn.execute("SELECT metadata_json FROM memories")))
    assert PAT not in str(list(store.conn.execute("SELECT reason FROM memory_revisions")))


def test_door3_the_default_mcp_manage_confirm_is_covered(tmp_path) -> None:
    context = _sqlite_context(tmp_path)
    pending = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={"title": "deploy day", "canonical_text": "Deploys may move to Wednesdays.", "confidence": 0.8},
    )
    assert pending["status"] == "confirmation_required"
    with pytest.raises(MCPToolError, match="credential material"):
        call_mcp_tool(
            context,
            name="alice_memory_manage",
            arguments={
                "action": "confirm",
                "confirmation_id": pending["confirmation_id"],
                "canonical_text": f"use {PAT} for deploys",
            },
        )
    with sqlite_user_connection(Path(tmp_path) / "memory.db", USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert [row[1] for row in _rows(store)] == ["needs_review"]
        assert _stored_text_anywhere(store, PAT) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "edit-and-approve", "body": {"text": f"use {PAT} for deploys"}},
        {"action": "supersede-existing", "replacement_body": {"text": f"use {PAT} for deploys"}},
    ],
)
def test_door3_the_mcp_review_edit_is_covered(tmp_path, arguments: dict[str, object]) -> None:
    context = _sqlite_context(tmp_path)
    committed = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={"title": "deploy", "canonical_text": "Deploys go out on Tuesdays."},
    )
    memory_id = committed["memory"]["id"]
    with pytest.raises(MCPToolError, match="credential material"):
        call_mcp_tool(context, name="alice_memory_correct", arguments={"review_item_id": memory_id, **arguments})
    with sqlite_user_connection(Path(tmp_path) / "memory.db", USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert [(row[1], row[3]) for row in _rows(store)] == [("active", "Deploys go out on Tuesdays.")]
        assert _stored_text_anywhere(store, PAT) == []


# ---------------------------------------------------------------------------
# Door 4, unit level. The end-to-end tests are in tests/integration.
# ---------------------------------------------------------------------------


def test_door4_generate_persists_no_candidate_that_carries_a_credential() -> None:
    store = MemoryMutationStoreStub()
    # Guards the guard: the stub does persist a clean decision, so an empty
    # store below means refused, not "the stub never writes".
    generate_memory_operation_candidates(
        store,  # type: ignore[arg-type]
        user_id=store.user_id,
        request=MemoryOperationGenerateInput(
            user_content="Decision: ship on Tuesdays", assistant_content="", mode="assist"
        ),
    )
    assert len(store.memory_operation_candidates) == 1

    store = MemoryMutationStoreStub()
    with pytest.raises(MemoryMutationValidationError, match="credential material"):
        generate_memory_operation_candidates(
            store,  # type: ignore[arg-type]
            user_id=store.user_id,
            request=MemoryOperationGenerateInput(
                user_content=f"Decision: rotate the deploy token to {PAT}",
                assistant_content="",
                mode="assist",
            ),
        )
    assert store.memory_operation_candidates == {}


class _RecordingContinuityStore:
    """Records whether anything was asked of the store."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        def record(*_args: object, **_kwargs: object) -> object:
            self.calls.append(name)
            raise LookupError(f"store.{name} reached")

        return record


def test_door4_the_continuity_object_sink_refuses_before_the_store_is_touched() -> None:
    kwargs = {
        "user_id": uuid4(),
        "capture_event_id": uuid4(),
        "object_type": "Decision",
        "provenance": {"source_kind": "continuity_capture_candidate"},
        "confidence": 0.98,
    }
    store = _RecordingContinuityStore()
    with pytest.raises(ContinuityObjectValidationError, match="credential material"):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            title="Decision: rotate the deploy token",
            body={"decision_text": f"rotate the deploy token to {PAT}"},
            **kwargs,  # type: ignore[arg-type]
        )
    assert store.calls == []

    # Guards the guard: clean text does reach the store.
    with pytest.raises(LookupError):
        create_continuity_object_record(
            store,  # type: ignore[arg-type]
            title="Decision: ship on Tuesdays",
            body={"decision_text": "ship on Tuesdays"},
            **kwargs,  # type: ignore[arg-type]
        )
    assert store.calls == ["create_continuity_object"]


class _StoredContinuityObject:
    """One stored continuity object. The lookup works; every write is recorded
    and then refused with LookupError, so a test sees whether a write was tried."""

    def __init__(self, *, title: str = "Decision: ship on Tuesdays", body: dict[str, object] | None = None) -> None:
        self.object_id = uuid4()
        self.row: dict[str, object] = {
            "id": self.object_id,
            "user_id": uuid4(),
            "capture_event_id": uuid4(),
            "object_type": "Decision",
            "status": "active",
            "is_preserved": True,
            "is_searchable": True,
            "is_promotable": True,
            "title": title,
            "body": body if body is not None else {"decision_text": "ship on Tuesdays"},
            "provenance": {"source_kind": "continuity_capture_event"},
            "confidence": 0.9,
            "last_confirmed_at": None,
            "supersedes_object_id": None,
            "superseded_by_object_id": None,
        }
        self.writes: list[str] = []

    def get_continuity_object_optional(self, object_id: object) -> dict[str, object] | None:
        return dict(self.row) if object_id == self.object_id else None

    def __getattr__(self, name: str) -> object:
        def record(*_args: object, **_kwargs: object) -> object:
            self.writes.append(name)
            raise LookupError(f"store.{name} reached")

        return record


def _correct(store: _StoredContinuityObject, request: ContinuityCorrectionInput) -> object:
    return apply_continuity_correction(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        continuity_object_id=store.object_id,
        request=request,
    )


@pytest.mark.parametrize(
    "request_input",
    [
        ContinuityCorrectionInput(action="edit", body={"decision_text": f"rotate to {PAT}"}),
        ContinuityCorrectionInput(action="supersede", replacement_title=f"Decision: rotate to {PAT}"),
    ],
)
def test_door4_continuity_correction_refuses_before_anything_is_written(
    request_input: ContinuityCorrectionInput,
) -> None:
    store = _StoredContinuityObject()
    with pytest.raises(ContinuityReviewValidationError, match="credential material"):
        _correct(store, request_input)
    assert store.writes == []

    # Guards the guard: a clean edit does reach the first write.
    with pytest.raises(LookupError):
        _correct(store, ContinuityCorrectionInput(action="edit", body={"decision_text": "ship on Fridays"}))
    assert store.writes == ["create_continuity_correction_event"]


def test_door4_a_title_only_edit_is_read_against_the_stored_body() -> None:
    """Design ruling T1-F2: the row as it will be stored, not just the request."""

    store = _StoredContinuityObject(body={"decision_text": "IAIOSFODNN7EXAMPLE is the deploy key id"})
    with pytest.raises(ContinuityReviewValidationError, match="credential material"):
        _correct(store, ContinuityCorrectionInput(action="edit", title="id AK"))
    assert store.writes == []


def test_door4_provenance_is_read_by_value_only() -> None:
    """Owner ruling C3, and the phase 9 eval's false positive of 2026-09-22.

    An OpenClaw import writes ``openclaw_dedupe_key`` holding a SHA-256 digest
    into provenance, and every correction of an imported object was refused.
    Provenance is read by value. The cost is pinned as a residual: a secret
    under a secret-shaped key whose value does not identify itself is not
    caught in provenance, although the same pair in a body is.
    """

    digest = hashlib.sha256(b"workspace").hexdigest()
    keyed_secret = {"api_key": "Xq9mZt2L" + "xP9wKc4BVq7m"}
    # Guards the guard: the pair rule does catch it when it is read keyed.
    assert carries_credential_material(keyed_secret)

    for provenance in ({"openclaw_dedupe_key": digest, "source_kind": "openclaw_import"}, keyed_secret):
        store = _StoredContinuityObject()
        with pytest.raises(LookupError):
            _correct(
                store,
                ContinuityCorrectionInput(
                    action="supersede", replacement_title="Decision: keep it", replacement_provenance=provenance
                ),
            )
        assert store.writes, "the write reached the store"
        sink = _RecordingContinuityStore()
        with pytest.raises(LookupError):
            create_continuity_object_record(
                sink,  # type: ignore[arg-type]
                user_id=uuid4(),
                capture_event_id=uuid4(),
                object_type="Decision",
                title="Decision: keep it",
                body={"decision_text": "keep it"},
                provenance=provenance,
                confidence=0.9,
            )
        assert sink.calls == ["create_continuity_object"]

    # A self-identifying token in a provenance value is still refused.
    store = _StoredContinuityObject()
    with pytest.raises(ContinuityReviewValidationError, match="credential material"):
        _correct(
            store,
            ContinuityCorrectionInput(
                action="edit", body={"decision_text": "keep it"}, provenance={"note": f"deployed with {PAT}"}
            ),
        )
    assert store.writes == []


# ---------------------------------------------------------------------------
# Door 5: artifact promotion.
# ---------------------------------------------------------------------------


class _ArtifactsOverSqlite:
    """Artifacts in memory, memories in a real SQLite store.

    SQLite has no artifact table, so the artifact side is a stub; the memory
    side is the real store, which is where the assertion is made.
    """

    def __init__(self, memories: SQLiteVNextStore, content: str) -> None:
        self.memories = memories
        self.artifacts = {
            "artifact-1": {
                "id": "artifact-1",
                "status": "draft",
                "title": "Deploy notes",
                "content_markdown": content,
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {},
            }
        }
        self.events: list[dict[str, object]] = []

    def get_artifact_for_update(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.artifacts.get(artifact_id)

    def update_artifact_status(self, *, artifact_id: str, status: str, **_kwargs: object) -> dict[str, object]:
        self.artifacts[artifact_id] = {**self.artifacts[artifact_id], "status": status}
        return self.artifacts[artifact_id]

    def create_memory(self, memory: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        return self.memories.create_memory(memory, actor_type=actor_type)

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def list_events(self, **_kwargs: object) -> list[dict[str, object]]:
        return []


def test_door5_artifact_promotion_refuses_a_credential_and_creates_no_memory() -> None:
    # Guards the guard: the same fixture with clean content does create a
    # real memory row, so the empty store below is a refusal.
    clean_store = _store()
    promoted = VNextQueueService(_ArtifactsOverSqlite(clean_store, "Deploys go out on Tuesdays.")).review_artifact(  # type: ignore[arg-type]
        artifact_id="artifact-1", action="promote"
    )
    assert [row[1] for row in _rows(clean_store)] == ["active"]
    assert promoted["status"] == "promoted_to_memory"

    store = _store()
    queue_store = _ArtifactsOverSqlite(store, f"Deploy with {PAT}")
    with pytest.raises(VNextQueueValidationError, match="credential material"):
        VNextQueueService(queue_store).review_artifact(artifact_id="artifact-1", action="promote")  # type: ignore[arg-type]

    assert _rows(store) == []
    assert queue_store.artifacts["artifact-1"]["status"] == "draft"


# ---------------------------------------------------------------------------
# Door 6: alice-memory import.
# ---------------------------------------------------------------------------


def _sqlite_context(tmp_path) -> MCPRuntimeContext:
    db_path = Path(tmp_path) / "memory.db"
    if not db_path.exists():
        bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(db_path), user_id=USER_ID)  # type: ignore[arg-type]


def _export_with_memory_text(tmp_path: Path, text: str | None, field: str = "canonical_text") -> tuple[Path, str]:
    """A real export whose one memory row is rewritten and the footer resealed."""

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        committed = _commit(store, title="deploy", canonical_text="Deploys go out on Tuesdays.")
        memory_id = str(committed["memory"]["id"])  # type: ignore[index]
    dump = tmp_path / "dump.jsonl"
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(dump)]) == 0
    envelopes = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    if text is not None:
        for payload in envelopes[1:-1]:
            if payload["record_type"] != "memory":
                continue
            if field == "value":
                value = payload["record"]["value"]
                decoded = json.loads(value) if isinstance(value, str) else dict(value)
                decoded["text"] = text
                payload["record"]["value"] = json.dumps(decoded) if isinstance(value, str) else decoded
            else:
                payload["record"][field] = text
    digest = hashlib.sha256()
    for payload in envelopes[1:-1]:
        digest.update(
            (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        )
    envelopes[-1]["record"]["sha256"] = digest.hexdigest()
    crafted = tmp_path / "crafted.jsonl"
    crafted.write_text(
        "".join(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            for payload in envelopes
        ),
        encoding="utf-8",
    )
    return crafted, memory_id


def test_door6_import_refuses_a_crafted_export_and_writes_nothing(tmp_path, capsys) -> None:
    # Guards the guard: the resealed export with ordinary text imports, so a
    # refusal below is the floor and not a broken footer.
    control, memory_id = _export_with_memory_text(tmp_path / "control", "Deploys go out on Wednesdays.")
    control_target = tmp_path / "control-target.db"
    assert onramp_main(["import", "--in", str(control), "--db", str(control_target), "--user-id", USER_ID]) == 0
    with sqlite_user_connection(control_target, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).get_memory(memory_id)["canonical_text"] == (  # type: ignore[index]
            "Deploys go out on Wednesdays."
        )
    capsys.readouterr()

    crafted, memory_id = _export_with_memory_text(tmp_path / "crafted", f"use {PAT} for deploys")
    target = tmp_path / "target.db"
    assert onramp_main(["import", "--in", str(crafted), "--db", str(target), "--user-id", USER_ID]) == 1
    err = capsys.readouterr().err
    errors = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert [error["error"]["code"] for error in errors] == ["import_credential_material"]
    assert not target.exists()
    # Addendum F3 and ruling C4: the user sees the line and the memory id,
    # the fields, and the per-backend fix; never the matched text.
    memory_line = next(
        index + 1
        for index, line in enumerate(crafted.read_text(encoding="utf-8").splitlines())
        if json.loads(line)["record_type"] == "memory"
    )
    assert f"line {memory_line}: memory {memory_id} carries credential material (canonical_text)" in err
    assert PAT not in err
    message = errors[0]["error"]["message"]
    assert "alice_memory_manage" in message and "alicebot vnext memories redact" in message
    assert "Forget or correct is not enough" in message and "export again" in message


def test_door6_import_lists_every_offender_in_one_pass(tmp_path, capsys) -> None:
    crafted, first_id = _export_with_memory_text(tmp_path, f"use {PAT} for deploys")
    envelopes = [json.loads(line) for line in crafted.read_text(encoding="utf-8").splitlines()]
    memory = next(payload for payload in envelopes if payload["record_type"] == "memory")
    second = json.loads(json.dumps(memory))
    second_id = str(uuid4())
    second["record"]["id"] = second_id
    second["record"]["memory_key"] = f"agentic_memory.semantic.{second_id}"
    second["record"]["commit_digest"] = None
    second["record"]["title"] = f"second {PAT}"
    legacy = tmp_path / "legacy.jsonl"
    body = [payload for payload in envelopes[1:-1]] + [second]
    legacy.write_text("".join(json.dumps(payload) + "\n" for payload in body), encoding="utf-8")

    assert onramp_main(["import", "--in", str(legacy), "--db", str(tmp_path / "t.db"), "--user-id", USER_ID]) == 1
    err = capsys.readouterr().err
    assert f"memory {first_id} carries credential material" in err
    assert f"memory {second_id} carries credential material (title, canonical_text)" in err
    assert PAT not in err


def _plant_active_row(conn: object, store: SQLiteVNextStore, **fields: object) -> dict[str, object]:
    """An active row holding credential text, as a vault written before the
    floor holds one: created as a candidate, then activated by SQL."""

    planted = store.create_memory({"memory_key": f"legacy.plant.{uuid4().hex[:8]}", "status": "candidate", **fields})
    conn.execute("UPDATE memories SET status = 'active' WHERE id = ?", (str(planted["id"]),))  # type: ignore[attr-defined]
    return {**planted, "status": "active"}


def test_door6_export_warns_about_a_row_import_will_refuse(tmp_path, capsys) -> None:
    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        clean = _commit(store, title="deploy", canonical_text="Deploys go out on Tuesdays.")
        # A row stored before the floor existed. The store now refuses to
        # create it active (ruling C2), so it is created as a candidate and
        # activated in SQL, the way an old vault holds it.
        planted = _plant_active_row(conn, store, title="token", canonical_text=f"use {PAT}")
    capsys.readouterr()
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(tmp_path / "d.jsonl")]) == 0
    err = capsys.readouterr().err
    assert f"memory {planted['id']} carries credential material" in err
    assert f"memory {clean['memory']['id']}" not in err  # type: ignore[index]
    assert "alice-memory import will refuse this export" in err
    assert PAT not in err


@pytest.mark.parametrize("field", ["title", "summary", "value"])
def test_door6_import_reads_every_rendered_field(tmp_path, capsys, field: str) -> None:
    crafted, _memory_id = _export_with_memory_text(tmp_path, f"use {PAT} for deploys", field=field)
    target = tmp_path / "target.db"
    assert onramp_main(["import", "--in", str(crafted), "--db", str(target), "--user-id", USER_ID]) == 1
    errors = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [error["error"]["code"] for error in errors] == ["import_credential_material"]
    assert not target.exists()


# ---------------------------------------------------------------------------
# Addendum F3 (2026-09-23): the in-vault fix works end to end. Import scans
# every memory row whatever its status, correction history included
# (metadata_json corrections[].previous_text), so correct() and forget() leave
# a backup import will still refuse; redact is the supported purge. These
# tests run the path a user is told to take: export, see the refusal with
# line numbers and ids, redact in the source vault, export again, import.
# ---------------------------------------------------------------------------


def _export(origin: Path, out: Path) -> None:
    assert onramp_main(["export", "--db", str(origin), "--user-id", USER_ID, "--out", str(out)]) == 0


@pytest.mark.parametrize("retire", ["correct", "forget"])
def test_f3_correct_or_forget_is_not_enough_and_redact_then_export_restores(
    tmp_path, capsys, monkeypatch, retire: str
) -> None:
    from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV

    origin = tmp_path / "origin.db"
    bootstrap_database(origin, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(origin, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        _commit(store, title="deploy", canonical_text="Deploys go out on Tuesdays.")
        planted = _plant_active_row(conn, store, title="deploy token", canonical_text=f"use {PAT} for deploys")
        service = VNextMemoryCommitService(store)
        if retire == "correct":
            service.correct(identity=None, memory_id=str(planted["id"]), canonical_text="Deploys use the vault token.")
        else:
            service.forget(identity=None, memory_id=str(planted["id"]), reason="rotated")

    first = tmp_path / "first.jsonl"
    _export(origin, first)
    capsys.readouterr()
    refused_target = tmp_path / "refused.db"
    assert onramp_main(["import", "--in", str(first), "--db", str(refused_target), "--user-id", USER_ID]) == 1
    err = capsys.readouterr().err
    line_no = next(
        index
        for index, line in enumerate(first.read_text(encoding="utf-8").splitlines(), start=1)
        if json.loads(line)["record_type"] == "memory" and json.loads(line)["record"]["id"] == str(planted["id"])
    )
    assert f"line {line_no}: memory {planted['id']} carries credential material" in err
    assert "Forget or correct is not enough" in err
    assert "alice_memory_manage with action=redact" in err
    assert PAT not in err
    assert not refused_target.exists()

    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(origin), user_id=USER_ID)  # type: ignore[arg-type]
    call_mcp_tool(
        context,
        name="alice_memory_manage",
        arguments={"action": "redact", "memory_id": str(planted["id"]), "reason": "held a deploy token"},
    )

    second = tmp_path / "second.jsonl"
    _export(origin, second)
    assert PAT not in second.read_text(encoding="utf-8")
    restored = tmp_path / "restored.db"
    assert onramp_main(["import", "--in", str(second), "--db", str(restored), "--user-id", USER_ID]) == 0
    with sqlite_user_connection(restored, USER_ID) as conn:
        texts = [str(row["canonical_text"]) for row in conn.execute("SELECT canonical_text FROM memories")]
    assert "Deploys go out on Tuesdays." in texts
    assert not any(PAT in text for text in texts)


def _import_one_memory(tmp_path: Path, **record_fields: object) -> tuple[int, str]:
    """Import a legacy (headerless) export holding one memory with these fields."""

    crafted, memory_id = _export_with_memory_text(tmp_path, None)
    envelopes = [json.loads(line) for line in crafted.read_text(encoding="utf-8").splitlines()]
    for payload in envelopes:
        if payload["record_type"] == "memory":
            payload["record"].update(record_fields)
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text("".join(json.dumps(payload) + "\n" for payload in envelopes[1:-1]), encoding="utf-8")
    code = onramp_main(["import", "--in", str(legacy), "--db", str(tmp_path / "target.db"), "--user-id", USER_ID])
    return code, memory_id


def test_round2_import_reads_the_value_by_value_and_metadata_as_a_mapping(tmp_path, capsys) -> None:
    """Import door. Round 2 read the value column keyed, as the design spec
    had it; round 3 (P2 item 7) follows owner ruling C3 instead: the value
    column is read by value only, so a key recognisable only by its name
    there is not caught (a documented residual), while a self-identifying
    key is. metadata_json stays keyed."""

    code, memory_id = _import_one_memory(tmp_path / "value", value={"text": "notes", "api_key": "Xq9mZt2L" + "xP9wKc4BVq7m"})
    assert code == 0, capsys.readouterr().err
    code, memory_id = _import_one_memory(tmp_path / "prefixed", value={"text": "notes", "note": PAT})
    err = capsys.readouterr().err
    assert code == 1 and f"memory {memory_id} carries credential material (value)" in err
    code, memory_id = _import_one_memory(tmp_path / "meta", metadata_json={"stripe_key": "Xq9mZt2L" + "xP9wKc4BVq7mZt2"})
    err = capsys.readouterr().err
    assert code == 1 and f"memory {memory_id} carries credential material (metadata_json)" in err
    # Guards the guard: structural keys in the same columns import.
    code, _memory_id = _import_one_memory(
        tmp_path / "clean", value={"text": "notes", "fact_key": "release_mode"}, metadata_json={"dedupe_key": HEX64}
    )
    assert code == 0, capsys.readouterr().err


def test_round2_import_leaves_a_preview_summary_out_of_the_join(tmp_path, capsys) -> None:
    """A "..." summary is a transformed copy the floor cannot recognise, so the
    import door leaves it out; otherwise the text's end meets its own start."""

    text = "IOSFODNN7EXAMPLE rollout notes " + "rollout notes " * 20 + "AKIA"
    summary = text[:277].rstrip() + "..."
    # Guards the guard: read with the preview, the row's own seam is a key.
    assert carries_credential_material(text, summary)
    code, _memory_id = _import_one_memory(tmp_path, canonical_text=text, summary=summary, title="Rollout")
    assert code == 0, capsys.readouterr().err


def test_door6_import_refuses_into_an_existing_vault_without_changing_it(tmp_path, capsys) -> None:
    crafted, memory_id = _export_with_memory_text(tmp_path / "crafted", f"use {PAT} for deploys")
    target = tmp_path / "existing.db"
    bootstrap_database(target, user_id=USER_ID, user_email="local@alice")
    with sqlite_user_connection(target, USER_ID) as conn:
        _commit(SQLiteVNextStore(conn, USER_ID), title="kept", canonical_text="The existing vault keeps this.")
    with sqlite_user_connection(target, USER_ID) as conn:
        before = _rows(SQLiteVNextStore(conn, USER_ID))

    assert onramp_main(["import", "--in", str(crafted), "--db", str(target), "--user-id", USER_ID]) == 1
    capsys.readouterr()
    with sqlite_user_connection(target, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert _rows(store) == before
        assert store.get_memory(memory_id) is None
        assert _stored_text_anywhere(store, PAT) == []


# ---------------------------------------------------------------------------
# Door 7: the detector is linear, and the rewrite matches what it replaced.
# ---------------------------------------------------------------------------

# The expression secret_assignment_values replaced, kept only as an oracle.
# Never run it on long input: it is the quadratic one.
_OLD_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?:[A-Za-z0-9]+[_-])*"
    r"(?:[A-Za-z0-9]*(?:password|passwd|secret|token|credentials?|apikey)|key)"
    r"(?:[_-][A-Za-z0-9]+)*"
    r"[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[A-Za-z0-9_\-+/=.]{6,})",
    re.IGNORECASE,
)
_OLD_JWT_PATTERN = re.compile(r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}")


def _existing_test_corpus() -> list[str]:
    """Every string literal in the floor's existing tests and the promotion corpus."""

    strings: list[str] = []
    for relative in ("tests/unit/test_vnext_promotion_policy.py", "tests/unit/fixtures_promotion_corpus.py"):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        strings.extend(
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    strings.extend(text for _title, text, *_rest in ALL_NOTES)
    return strings


def _assert_same_as_the_oracles(text: str) -> None:
    old_values = [match.group("value") for match in _OLD_SECRET_ASSIGNMENT_PATTERN.finditer(text)]
    assert list(policy.secret_assignment_values(text)) == old_values, text
    assert credential_floor._matches_jwt(text) is (_OLD_JWT_PATTERN.search(text) is not None), text


def test_door7_the_linear_scanners_agree_with_the_old_regexes_on_the_existing_corpus() -> None:
    corpus = _existing_test_corpus()
    # Guards the guard: the corpus really contains assignment and JWT
    # shapes, so agreement is not agreement on nothing.
    assert len(corpus) > 1000
    assert sum(1 for text in corpus if _OLD_SECRET_ASSIGNMENT_PATTERN.search(text)) > 30
    assert any(_OLD_JWT_PATTERN.search(text) for text in corpus)
    for text in corpus:
        _assert_same_as_the_oracles(text)


@pytest.mark.parametrize(
    ("text", "values"),
    [
        # finditer resumes after a value, so a name inside a consumed value is
        # never read. The old rule therefore misses "token:Hunter22" here, and
        # the rewrite keeps that miss rather than quietly widening the rule.
        ("token=abcdefgtoken:Hunter22", ["abcdefgtoken"]),
        ("token=abcdef token=Hunter22", ["abcdef", "Hunter22"]),
        # A name followed by an empty segment is not a name. Also an old miss,
        # also kept.
        ("password_=hunter2abc", []),
        ("api_key_=Abc123def", []),
        ("key__x=Abc123def", []),
        # Leading separators and quotes still open and close an assignment.
        ("__password=hunter2abc", ["hunter2abc"]),
        ("api-key = 'Abc123def'", ["Abc123def"]),
        ('"secret_key": "' + 'abc123def456"', ["abc123def456"]),
    ],
)
def test_door7_the_scanner_keeps_the_old_rule_on_its_edge_shapes(text: str, values: list[str]) -> None:
    """Pinned literally, because the existing corpus has none of these shapes.

    Found by mutation: dropping the non-overlap skip, or the empty-segment
    rule, left the corpus comparison green and only the fuzz caught it.
    """

    assert [match.group("value") for match in _OLD_SECRET_ASSIGNMENT_PATTERN.finditer(text)] == values
    assert list(policy.secret_assignment_values(text)) == values


_FUZZ_WORDS = (
    "key", "KEY", "token", "password", "passwd", "secret", "credential", "credentials", "apikey",
    "api", "my", "x", "PG", "monkey", "keys", "\u017fecret", "to\u212aen", "\u212aey",
    "_", "-", "__", "=", ":", " ", "\t", "\n", "\u00a0", "'", '"', ".", "/", "+", ",",
    "abc123", "hunter2", "abcdef", "ABCDEF", "abcdefgh", "\u00e9", "eyJ", "eyJ-", "abcdefghij", "..",
    "eyJabcdefgh", ".abcdefgh", ".abcd", "eyJhbGciOi.abcdefgh.",
)


def test_door7_the_linear_scanners_agree_with_the_old_regexes_on_a_seeded_fuzz_corpus() -> None:
    rng = random.Random(20260922)
    assignment_matches = jwt_matches = 0
    for _ in range(20_000):
        text = "".join(rng.choice(_FUZZ_WORDS) for _ in range(rng.randint(1, 14)))
        _assert_same_as_the_oracles(text)
        assignment_matches += bool(_OLD_SECRET_ASSIGNMENT_PATTERN.search(text))
        jwt_matches += bool(_OLD_JWT_PATTERN.search(text))
    assert assignment_matches > 500
    assert jwt_matches > 0


_ADVERSARIAL_50KB = {
    "underscore run": "a_" * 25_000,
    "hyphen run": "a-" * 25_000,
    "key segments": "key_" * 12_500,
    "jwt starts": "eyJ-" * 12_500,
}

# Measured on the build machine on 2026-09-22: the whole choke point takes
# 0.05 to 0.2 s per 50 KB field. Before the fix one scan of the assignment
# regex alone took 43 s on the underscore run and looks_like_credential took
# 174 s. Two seconds leaves roughly ten times headroom for a slow CI runner
# and still fails the quadratic code by more than an order of magnitude.
_CHOKE_POINT_BUDGET_SECONDS = 2.0


@pytest.mark.parametrize("label", sorted(_ADVERSARIAL_50KB))
def test_door7_adversarial_50kb_input_scans_fast(label: str) -> None:
    text = _ADVERSARIAL_50KB[label]
    started = time.perf_counter()
    assert carries_credential_material("title", text) is False
    elapsed = time.perf_counter() - started
    assert elapsed < _CHOKE_POINT_BUDGET_SECONDS, (label, elapsed)

    # Guards the guard: the same path reads to the end of the run and still
    # finds a real assignment placed after it.
    started = time.perf_counter()
    assert carries_credential_material("title", text + " api_key=" + "Abc123def456") is True
    assert time.perf_counter() - started < _CHOKE_POINT_BUDGET_SECONDS


def test_door7_the_scanners_are_linear_not_just_fast_at_one_size() -> None:
    """200 KB through the raw scanners, which took 0.3 to 1.2 ms at 50 KB."""

    for text in _ADVERSARIAL_50KB.values():
        big = text * 4
        started = time.perf_counter()
        list(policy.secret_assignment_values(big))
        credential_floor._matches_jwt(big)
        assert time.perf_counter() - started < 0.5


def test_door7_commit_source_refs_are_bounded_at_the_service() -> None:
    """Owner ruling R4 (2026-09-23): a string ref is measured as sent.

    Round 1 measured every ref by its JSON form, which counted each quote
    and newline twice, so a 4,000 character ref of quotes was refused.
    """

    base = {"title": "deploy", "canonical_text": "Deploys go out on Tuesdays."}
    memory_commit_request_from_payload({**base, "source_refs": ["ref"] * MAX_COMMIT_SOURCE_REFS}, user_id=USER_ID)
    memory_commit_request_from_payload({**base, "source_refs": ["x" * MAX_COMMIT_SOURCE_REF_CHARS]}, user_id=USER_ID)
    for heavy in ('"' * MAX_COMMIT_SOURCE_REF_CHARS, "\n" * MAX_COMMIT_SOURCE_REF_CHARS, '"\n' * 2_000):
        assert len(heavy) == MAX_COMMIT_SOURCE_REF_CHARS
        memory_commit_request_from_payload({**base, "source_refs": [heavy]}, user_id=USER_ID)
    with pytest.raises(VNextMemoryCommitValidationError, match="at most 64"):
        memory_commit_request_from_payload(
            {**base, "source_refs": ["ref"] * (MAX_COMMIT_SOURCE_REFS + 1)}, user_id=USER_ID
        )
    with pytest.raises(VNextMemoryCommitValidationError, match="at most 4000"):
        memory_commit_request_from_payload(
            {**base, "source_refs": ["x" * (MAX_COMMIT_SOURCE_REF_CHARS + 1)]}, user_id=USER_ID
        )
    # A non-string ref is measured by its serialized length.
    with pytest.raises(VNextMemoryCommitValidationError, match="at most 4000"):
        memory_commit_request_from_payload(
            {**base, "source_refs": [{"nested": ["x" * MAX_COMMIT_SOURCE_REF_CHARS]}]}, user_id=USER_ID
        )


@pytest.mark.parametrize("tool", ["alice_memory_commit", "alice_vnext_commit_memory"])
def test_door7_both_mcp_commit_tools_bound_source_refs_on_the_raw_argument(tmp_path, monkeypatch, tool: str) -> None:
    from alicebot_api.surface_flags import MCP_LEGACY_TOOLS_ENV

    monkeypatch.setenv(MCP_LEGACY_TOOLS_ENV, "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    context = _sqlite_context(tmp_path)
    arguments: dict[str, object] = {"title": "deploy", "canonical_text": "Deploys go out on Tuesdays."}
    if tool == "alice_vnext_commit_memory":
        arguments["agent_id"] = "hermes"
    # The schema layer's own message, which the service's error does not
    # carry, so this pins the raw-argument check and not the service one.
    with pytest.raises(MCPToolError, match=r"has invalid value at arguments\.source_refs\[0\]: must be at most 4000 characters"):
        call_mcp_tool(
            context, name=tool, arguments={**arguments, "source_refs": ["x" * (MAX_COMMIT_SOURCE_REF_CHARS + 1)]}
        )
    with pytest.raises(MCPToolError, match=r"has invalid value at arguments\.source_refs"):
        call_mcp_tool(context, name=tool, arguments={**arguments, "source_refs": ["ref"] * (MAX_COMMIT_SOURCE_REFS + 1)})
    for ref in ("x" * MAX_COMMIT_SOURCE_REF_CHARS, '"\n' * 2_000):
        result = call_mcp_tool(context, name=tool, arguments={**arguments, "source_refs": [ref]})
        assert result["status"] in {"committed", "duplicate"}, result


def test_door7_the_cli_commit_bounds_source_refs_through_the_service(monkeypatch) -> None:
    import argparse
    from contextlib import contextmanager

    from alicebot_api.cli import memories as cli_memories

    store = _store()

    @contextmanager
    def fake_store_context(_ctx: object):
        yield store

    monkeypatch.setattr(cli_memories, "_vnext_store_context", fake_store_context)
    monkeypatch.setattr(cli_memories, "_persist_deferred_embedding_inputs", lambda *args, **kwargs: None)

    def run(refs: list[str]) -> str:
        args = argparse.Namespace(
            agent_id=None, agent_type=None, permission_profile=None, agent_run_id=None, task_id=None,
            project_scope=[], sensitivity_allowed=None, title="deploy", text="Deploys go out on Tuesdays.",
            memory_type="semantic", domain="unknown", sensitivity="unknown", confidence=0.9,
            intent="explicit_remember", source_type="direct_user_instruction", source_ref=refs,
            conversation_excerpt=None, rationale=None, idempotency_key=None, contradiction_ref=[],
        )
        return cli_memories._run_vnext_memory_commit(argparse.Namespace(user_id=store.user_id), args)  # type: ignore[arg-type]

    with pytest.raises(VNextMemoryCommitValidationError, match="at most 4000"):
        run(["x" * (MAX_COMMIT_SOURCE_REF_CHARS + 1)])
    assert json.loads(run(['"\n' * 2_000]))["status"] == "committed"


def test_door7_the_http_model_and_mcp_schemas_carry_the_service_numbers() -> None:
    from pydantic import ValidationError

    from alicebot_api import write_bounds
    from alicebot_api.mcp import definitions
    from alicebot_api.routers.vnext_memories import VNextMemoryCommitRequest

    assert (MAX_COMMIT_SOURCE_REFS, MAX_COMMIT_SOURCE_REF_CHARS) == (
        write_bounds.MAX_COMMIT_SOURCE_REFS,
        write_bounds.MAX_COMMIT_SOURCE_REF_CHARS,
    ) == (64, 4_000)
    with pytest.raises(ValidationError):
        VNextMemoryCommitRequest(
            user_id=uuid4(), title="t", canonical_text="c", source_refs=["ref"] * (MAX_COMMIT_SOURCE_REFS + 1)
        )
    for catalog, name in (
        (definitions._CORE_TOOL_DEFINITIONS, "alice_memory_commit"),
        (definitions._LEGACY_TOOL_DEFINITIONS, "alice_vnext_commit_memory"),
    ):
        tool = next(tool for tool in catalog if tool["name"] == name)
        refs_schema = tool["inputSchema"]["properties"]["source_refs"]  # type: ignore[index]
        assert refs_schema["maxItems"] == MAX_COMMIT_SOURCE_REFS, name
        assert refs_schema["items"]["maxLength"] == MAX_COMMIT_SOURCE_REF_CHARS, name


# ---------------------------------------------------------------------------
# Owner ruling R5 (2026-09-23): the two agent-control patterns whose leading
# \s* ran across every later newline. Door 7's own definition, measured on
# 2026-09-22: about 20 s per bounded HTTP commit with a persona configured.
# The same harness as the scanners above: an oracle, a seeded fuzz, a budget
# and a linearity ratio. Only these two patterns are claimed linear, not the
# whole promotion evaluation.
# ---------------------------------------------------------------------------

_OLD_AGENT_CONTROL_1 = re.compile(r"^\s*(?:system|assistant)\s*:", re.IGNORECASE | re.MULTILINE)
_OLD_AGENT_CONTROL_4 = re.compile(r"^\s*#{2,}\s*instructions?\b", re.IGNORECASE | re.MULTILINE)
# Every character \s matches in a str pattern, as the fuzz alphabet must
# carry: CR, VT, FF, the \x1c to \x1f separators, NEL, NBSP, U+2028 and
# U+2029, U+3000, and the rest of the Unicode spaces.
_EVERY_WHITESPACE = "".join(chr(cp) for cp in range(0x110000) if re.fullmatch(r"\s", chr(cp)))


def _agent_control_pair() -> tuple[re.Pattern[str], re.Pattern[str]]:
    return policy._AGENT_CONTROL_PATTERNS[1], policy._AGENT_CONTROL_PATTERNS[4]


def _assert_agent_control_agrees(text: str) -> None:
    new_1, new_4 = _agent_control_pair()
    for surface in policy._surface_forms(text):
        assert bool(new_1.search(surface)) is bool(_OLD_AGENT_CONTROL_1.search(surface)), repr(surface)
        assert bool(new_4.search(surface)) is bool(_OLD_AGENT_CONTROL_4.search(surface)), repr(surface)


def test_r5_the_patterns_changed_only_their_leading_run() -> None:
    new_1, new_4 = _agent_control_pair()
    assert new_1.pattern == _OLD_AGENT_CONTROL_1.pattern.replace("^\\s*", "^[^\\S\\n]*", 1)
    assert new_4.pattern == _OLD_AGENT_CONTROL_4.pattern.replace("^\\s*", "^[^\\S\\n]*", 1)
    assert (new_1.flags, new_4.flags) == (_OLD_AGENT_CONTROL_1.flags, _OLD_AGENT_CONTROL_4.flags)
    # Guards the guard: the alphabet holds every character the fuzz needs.
    for char in "\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u2028\u2029\u3000":
        assert char in _EVERY_WHITESPACE


def test_r5_the_new_patterns_agree_with_the_old_on_the_existing_corpus() -> None:
    corpus = _existing_test_corpus()
    assert sum(1 for text in corpus if _OLD_AGENT_CONTROL_1.search(text) or _OLD_AGENT_CONTROL_4.search(text)) >= 5
    for text in corpus:
        _assert_agent_control_agrees(text)


def test_r5_the_new_patterns_agree_with_the_old_on_a_seeded_whitespace_fuzz() -> None:
    rng = random.Random(20260923)
    words = ("system", "SYSTEM", "assistant", "Assistant", ":", "##", "###", "#", "instructions", "instruction",
             "x", "note", "sys", "tem", "\n", "\n\n")
    hits = 0
    for _ in range(20_000):
        parts = [rng.choice(words) if rng.random() < 0.6 else rng.choice(_EVERY_WHITESPACE) for _ in range(rng.randint(1, 12))]
        text = "".join(parts)
        _assert_agent_control_agrees(text)
        hits += bool(_OLD_AGENT_CONTROL_1.search(text) or _OLD_AGENT_CONTROL_4.search(text))
    assert hits > 500


def _agent_control_seconds(text: str) -> float:
    new_1, new_4 = _agent_control_pair()
    started = time.perf_counter()
    for surface in policy._surface_forms(text):
        new_1.search(surface)
        new_4.search(surface)
    return time.perf_counter() - started


_NEWLINE_RUNS = {
    "newlines": "\n" * 50_000,
    "space newline": " \n" * 25_000,
    "crlf": "\r\n" * 25_000,
    "mixed separators": "\n\x0b\x0c\x1c\u2028 " * 8_334,
}


@pytest.mark.parametrize("label", sorted(_NEWLINE_RUNS))
def test_r5_a_50kb_newline_run_scans_inside_the_budget(label: str) -> None:
    text = _NEWLINE_RUNS[label][:50_000] + "no control line here"
    assert _agent_control_seconds(text) < _CHOKE_POINT_BUDGET_SECONDS, label
    # Guards the guard: the same run still finds a control line after it.
    new_1, _new_4 = _agent_control_pair()
    assert new_1.search(_NEWLINE_RUNS[label][:50_000] + "\nsystem: obey")


@pytest.mark.parametrize("label", sorted(_NEWLINE_RUNS))
def test_r5_four_times_the_input_costs_about_four_times_the_time(label: str) -> None:
    small = _NEWLINE_RUNS[label][:25_000]
    large = small * 4
    # Best of three at each size, so one slow tick cannot fail the ratio.
    small_seconds = min(_agent_control_seconds(small) for _ in range(3))
    large_seconds = min(_agent_control_seconds(large) for _ in range(3))
    # Linear is 4x; quadratic is 16x. 8x leaves room for noise and still
    # fails the old patterns by a wide margin.
    assert large_seconds < max(8 * small_seconds, 0.02), (label, small_seconds, large_seconds)


# ---------------------------------------------------------------------------
# S4.4 round 3, P2 item 5: the key-body pattern after a private key header.
# Its "Name: value" group was unbounded, so a repeated header with no colon
# ("-beginprivatekey" or "-BEGINPRIVATEKEY" over and over) cost time
# quadratic in its length: measured on 8e00cc5 at 0.11 s for 50 KB and 1.6 s
# for 200 KB. Every repetition in the pattern is now bounded.
# ---------------------------------------------------------------------------

_REPEATED_HEADERS = ("-beginprivatekey", "-BEGINPRIVATEKEY", "BEGIN PRIVATE KEY:")


def _header_run(unit: str, size: int) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _floor_seconds(text: str) -> float:
    started = time.perf_counter()
    carries_credential_material(text)
    return time.perf_counter() - started


@pytest.mark.parametrize("unit", _REPEATED_HEADERS)
def test_round3_a_repeated_private_key_header_scans_in_linear_time(unit: str) -> None:
    small = _header_run(unit, 50_000)
    large = _header_run(unit, 200_000)
    small_seconds = min(_floor_seconds(small) for _ in range(3))
    large_seconds = min(_floor_seconds(large) for _ in range(3))
    assert small_seconds < _CHOKE_POINT_BUDGET_SECONDS, unit
    # Linear is 4x; quadratic is 16x (measured 15x on 8e00cc5).
    assert large_seconds < max(8 * small_seconds, 0.05), (unit, small_seconds, large_seconds)


# Round 4: the paths added for wrapped base64, PuTTY files and mangled armor.
# The PuTTY run is the base64 of a PuTTY header line.
_ROUND4_PPK_HEAD = "UHVUVFktVXNlci1LZXktRmlsZS0yOiBzc2gtcnNhCkVu"
_ROUND4_SHAPES = {
    "40-char base64 runs over spaces": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNk ",
    "PuTTY header runs over spaces": _ROUND4_PPK_HEAD + " ",
    "PuTTY header runs over lines": _ROUND4_PPK_HEAD + "\n",
    "headers with line junk": "BEGIN PRIVATE KEY > # // <br> ",
    "em-dash headers": "\u2014BEGIN PRIVATE KEY\u2014",
    "PuTTY section names": "PuTTY-User-Key-File-2: Private-Lines: 1 > ",
}


def test_round4_the_putty_run_really_decodes_to_a_putty_header() -> None:
    import base64

    assert base64.b64decode(_ROUND4_PPK_HEAD).startswith(b"PuTTY-User-Key-File-2:")


@pytest.mark.parametrize("label", sorted(_ROUND4_SHAPES))
def test_round4_the_new_private_key_paths_scan_in_linear_time(label: str) -> None:
    small = _header_run(_ROUND4_SHAPES[label], 50_000)
    large = _header_run(_ROUND4_SHAPES[label], 200_000)
    small_seconds = min(_floor_seconds(small) for _ in range(3))
    large_seconds = min(_floor_seconds(large) for _ in range(3))
    assert small_seconds < _CHOKE_POINT_BUDGET_SECONDS, label
    assert large_seconds < max(8 * small_seconds, 0.05), (label, small_seconds, large_seconds)


# S4.4 round 5: the body after a header. Round 4's "Name: value" lines ended
# at any junk character, so four of them could split "a: a: a: ..." in
# combinatorially many ways: 2.4 to 4.8 s for a 1.2 KB note after one
# header and 9.6 to 19 s for 4.9 KB, measured on 6c24a97 on 2026-09-23.
# Each header line now ends at a forced point.
_ROUND5_SHAPES = {
    "colon words after a header line": "BEGIN PRIVATE KEY\n" + "a: " * 400,
    "colon words after armor dashes": "BEGIN PRIVATE KEY" + "-" * 5 + " " + "a: " * 400,
    "colon words after a quote prefix": "BEGIN PRIVATE KEY > " + "a: " * 400,
    "headers with dashes and a name": "BEGIN PRIVATE KEY" + "-" * 5 + " X: ",
    "folded header before short words": "BEGIN PRIVATE KEY Comment: x ",
}


@pytest.mark.parametrize("label", sorted(_ROUND5_SHAPES))
def test_round5_the_body_after_a_header_scans_in_linear_time(label: str) -> None:
    # One copy first, so the old pattern fails in seconds rather than hanging
    # on 50 KB: 1.2 KB of the first shape took 2.4 s before the fix.
    assert _floor_seconds(_ROUND5_SHAPES[label]) < 0.25, label
    small = _header_run(_ROUND5_SHAPES[label], 50_000)
    large = _header_run(_ROUND5_SHAPES[label], 200_000)
    small_seconds = min(_floor_seconds(small) for _ in range(3))
    large_seconds = min(_floor_seconds(large) for _ in range(3))
    assert small_seconds < _CHOKE_POINT_BUDGET_SECONDS, label
    assert large_seconds < max(8 * small_seconds, 0.05), (label, small_seconds, large_seconds)
    # Guards the guard: the repeated unit really holds the header whose body
    # pattern was the costly one.
    assert "BEGIN PRIVATE KEY" in small


# ---------------------------------------------------------------------------
# S4 CI (2026-09-23): CodeQL py/polynomial-redos, alerts 525 to 528 on PR #414.
# The camel-hump splitter ("may run slow on strings with many repetitions of
# 'A'") and the three secret-name suffix patterns ("... of '0'") in
# _name_kind. Measured as used, with fullmatch, the suffix patterns were
# linear; the same pattern under search took 4.7 s at 50 KB and 76 s at
# 200 KB of "0". Both are now built so they cannot backtrack. The old
# patterns are kept here as oracles, and the new code must agree with them.
# ---------------------------------------------------------------------------

_OLD_CAMEL_HUMP = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[0-9]+")
_OLD_SUFFIX_PATTERNS = {
    "password": (re.compile(r"[a-z0-9]*(?:password|passwd|passphrase)"), credential_floor._PASSWORD_WORDS),
    "secret": (
        re.compile(
            r"[a-z0-9]*(?:secret|credentials?|apikey|secretkey|accesskey|privatekey|signingkey|authtoken|accesstoken)"
        ),
        credential_floor._SECRET_WORDS,
    ),
    "token": (re.compile(r"[a-z0-9]*token"), credential_floor._TOKEN_WORDS),
}


@pytest.mark.parametrize(
    "piece",
    ["nextPageToken", "PGPASSWORD", "apiKey", "HTTPServer", "B64", "AB1c", "aB", "A", "ABc", "a1B2cD", "\u00e9Ab", ""],
)
def test_codeql_the_hump_splitter_keeps_the_old_splits(piece: str) -> None:
    assert credential_floor._camel_humps(piece) == _OLD_CAMEL_HUMP.findall(piece)


def test_codeql_the_hump_splitter_matches_the_old_pattern_on_a_seeded_fuzz() -> None:
    rng = random.Random(20260923)
    alphabet = "AZQabzq0 9_-.\u00e9\u00c9\u212a"
    joined_capital = 0
    for _ in range(20_000):
        piece = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        old = _OLD_CAMEL_HUMP.findall(piece)
        assert credential_floor._camel_humps(piece) == old, piece
        joined_capital += any(len(hump) > 1 and hump[0].isupper() and hump[1].islower() for hump in old)
    # Guards the guard: the capital handed to the next hump is exercised.
    assert joined_capital > 500


def test_codeql_the_suffix_tests_match_the_old_patterns() -> None:
    rng = random.Random(20260924)
    words = ("password", "passwd", "passphrase", "secret", "credential", "credentials", "apikey", "token",
             "accesstoken", "tok", "secre", "key")
    matches = 0
    for _ in range(20_000):
        parts = [rng.choice(("a", "0", "9", "z", "X", "_", rng.choice(words))) for _ in range(rng.randint(0, 5))]
        segment = "".join(parts)
        for pattern, words_for_kind in _OLD_SUFFIX_PATTERNS.values():
            old = pattern.fullmatch(segment) is not None
            assert credential_floor._segment_ends_in(segment, words_for_kind) is old, segment
            matches += old
    assert matches > 1_000


# The alerts' own inputs: a long run of "0" and of "A", as a NAME=value name,
# as a mapping key, and straight into _name_kind.
_CODEQL_UNITS = {"zeros": "0", "capitals": "A", "capital then lower": "Aa", "lower then digit": "a0"}


def _name_seconds(name: str) -> float:
    started = time.perf_counter()
    credential_floor._name_kind(name, "", 0)
    carries_credential_material(name + "=" + "Abc123def456")
    carries_credential_material({name: "Abc123def456"})
    return time.perf_counter() - started


@pytest.mark.parametrize("label", sorted(_CODEQL_UNITS))
def test_codeql_a_long_name_scans_in_linear_time(label: str) -> None:
    unit = _CODEQL_UNITS[label]
    small = unit * (50_000 // len(unit))
    large = unit * (200_000 // len(unit))
    small_seconds = min(_name_seconds(small) for _ in range(3))
    large_seconds = min(_name_seconds(large) for _ in range(3))
    assert large_seconds < budget(_CHOKE_POINT_BUDGET_SECONDS), (label, large_seconds, tracer_active())
    # Linear is 4x; quadratic is 16x.
    assert large_seconds < max(8 * small_seconds, 0.02), (label, small_seconds, large_seconds)
    # Guards the guard: the same long name ending in a secret word is read.
    assert credential_floor._name_kind(large + "_password", "", 0) == "password"
