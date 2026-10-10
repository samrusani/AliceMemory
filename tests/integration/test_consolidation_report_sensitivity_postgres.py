"""A report is read behind the label it was stored with, so the label must cover what it names.

A consolidation run that proposes only roll-up cards over confidential memories used to store its report with
sensitivity ``unknown``: the label was taken over the near-duplicate clusters, which such a run does not have. The
report prints the card topic and the ids of the memories behind it, so a ``trusted_local_agent`` key (a ceiling
below confidential) read both through ``GET /v0/vnext/artifacts/{id}``.

These tests run the real scheduler path on role-separated Postgres and read the stored report back over HTTP as a
key with a ceiling below the memories and as a key with no ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_review as vnext_review_router
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from alicebot_api.vnext_embeddings import pad_embedding_vector
from alicebot_api.vnext_scheduler import SchedulerRunRequest
from alicebot_api.vnext_scheduler_runtime import run_now_durable
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user

ALLOWED_WITH_CONFIDENTIAL = ("public", "internal", "private", "confidential", "unknown")
GAME_TEXTS = (
    ("I played Hollow Knight for 25 hours", "2023-06-02"),
    ("I played Stardew Valley for 85 hours", "2023-06-20"),
    ("I played Celeste for 10 hours", "2023-07-01"),
)


@pytest.fixture(autouse=True)
def _no_embedding_provider(monkeypatch):
    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _point_routes_at(monkeypatch, database_url: str) -> None:
    settings = Settings(database_url=database_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_review_router, "get_settings", lambda: settings)


def _seed_games(store: PostgresVNextStore, *, sensitivity: str, domain: str = "personal") -> list[dict]:
    rows = []
    for index, (text, day) in enumerate(GAME_TEXTS):
        rows.append(
            store.create_memory(
                {
                    "memory_key": f"games.{sensitivity}.{domain}.{index}.{uuid4().hex[:8]}",
                    "memory_type": "episode",
                    "title": text,
                    "canonical_text": text,
                    "summary": text,
                    "status": "active",
                    "value": {"text": text},
                    "domain": domain,
                    "sensitivity": sensitivity,
                    "metadata_json": {"session_date": day},
                }
            )
        )
    return rows


def _keys(database_url: str, user_id: UUID) -> tuple[str, str]:
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        _trusted_record, trusted_key = create_agent_key(
            store, user_id=user_id, agent_id="trusted-reader", permission_profile="trusted_local_agent"
        )
        _admin_record, admin_key = create_agent_key(
            store, user_id=user_id, agent_id="admin-reader", permission_profile="admin_agent"
        )
    return trusted_key, admin_key


def _get_artifact(artifact_id: str, user_id: UUID, key: str) -> tuple[int, dict]:
    return invoke_request(
        "GET",
        f"/v0/vnext/artifacts/{artifact_id}",
        authorization=f"Bearer {key}",
        query_params={"user_id": str(user_id)},
    )


def _run_consolidation(database_url: str, user_id: UUID) -> dict:
    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="memory_consolidation",
            sensitivity_allowed=ALLOWED_WITH_CONFIDENTIAL,
            generated_for=datetime.now(UTC).date().isoformat(),
        ),
    )
    assert result["run"]["status"] == "succeeded", result
    return result["artifact"]


def test_rollup_only_report_over_confidential_memories_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="rollup-report-label@example.com")
    with user_connection(database_url, user_id) as conn:
        members = _seed_games(PostgresVNextStore(conn), sensitivity="confidential")
    trusted_key, admin_key = _keys(database_url, user_id)

    artifact = _run_consolidation(database_url, user_id)

    rollups = artifact["metadata_json"]["rollups"]
    assert len(rollups["proposals"]) == 1, "the run must propose a roll-up card and nothing else"
    assert artifact["metadata_json"]["consolidation"]["cluster_membership"] == []
    assert "hours played" in artifact["content_markdown"]
    member_ids = {str(row["id"]) for row in members}
    assert member_ids <= set(rollups["groups"][0]["member_ids"])

    artifact_id = str(artifact["id"])
    status, body = _get_artifact(artifact_id, user_id, trusted_key)
    # The report is above the key's ceiling, so for the key it does not exist.
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    leaked = json.dumps(body)
    assert "hours played" not in leaked
    assert not any(member_id in leaked for member_id in member_ids)

    status, body = _get_artifact(artifact_id, user_id, admin_key)
    assert status == 200, body
    assert "hours played" in body["content_markdown"]
    assert artifact["sensitivity"] == "confidential"


def test_rollup_only_report_over_private_memories_is_refused_to_a_read_only_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """The same gap at the default ceiling: no opt-in to confidential is needed for a read-only key
    (public, internal, unknown) to read a topic built from private memories."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="rollup-report-private@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        _seed_games(store, sensitivity="private")
        _record, read_only_key = create_agent_key(
            store, user_id=user_id, agent_id="read-only-reader", permission_profile="read_only_agent"
        )
        _record, trusted_key = create_agent_key(
            store, user_id=user_id, agent_id="trusted-reader", permission_profile="trusted_local_agent"
        )

    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="memory_consolidation", generated_for=datetime.now(UTC).date().isoformat()
        ),
    )
    artifact = result["artifact"]
    assert len(artifact["metadata_json"]["rollups"]["proposals"]) == 1

    status, body = _get_artifact(str(artifact["id"]), user_id, read_only_key)
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    assert "hours played" not in json.dumps(body)
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 200, body
    assert artifact["sensitivity"] == "private"


def test_open_loop_review_naming_a_confidential_source_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """Same class in the open-loop review: the report prints the id of the source each loop links, and the loop
    can be less sensitive than its source."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="open-loop-review-label@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Counsel note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "personal",
                "sensitivity": "confidential",
            }
        )
        store.create_open_loop(
            {
                "title": "Reply to the filing",
                "status": "open",
                "domain": "personal",
                "sensitivity": "internal",
                "source_id": str(source["id"]),
            },
            actor_type="user",
        )
    trusted_key, admin_key = _keys(database_url, user_id)

    result = run_now_durable(
        database_url=database_url,
        user_id=user_id,
        request=SchedulerRunRequest(
            workflow_type="open_loop_review",
            sensitivity_allowed=ALLOWED_WITH_CONFIDENTIAL,
            generated_for=datetime.now(UTC).date().isoformat(),
        ),
    )
    artifact = result["artifact"]
    source_id = str(source["id"])
    assert result["run"]["status"] == "succeeded", result
    assert f"source:{source_id}" in artifact["content_markdown"]

    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    assert source_id not in json.dumps(body)

    status, body = _get_artifact(str(artifact["id"]), user_id, admin_key)
    assert status == 200, body
    assert f"source:{source_id}" in body["content_markdown"]
    assert artifact["sensitivity"] == "confidential"


class _OneVector:
    """An embedding provider that puts every text at the same point, so identical memories form one cluster."""

    provider = "test_embeddings"
    model = "test-embed-1"

    def embed_text(self, text: str) -> list[float]:
        # The width the vector column holds: a shorter vector makes the probe search fail inside the transaction.
        return list(pad_embedding_vector([0.5, 0.1, 0.2]))

    def embed_batch(self, texts) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]


def _consolidate_a_cluster_citing(database_url: str, user_id: UUID, *, source_fields: dict) -> tuple[dict, dict]:
    """Three near-duplicate internal memories that each cite one source, clustered and reported on Postgres.

    Returns the stored report and the source row. The members are ``personal`` and ``internal``: less sensitive than
    the source they cite when ``source_fields`` raise it.
    """

    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Clinic letter",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "personal",
                "sensitivity": "internal",
                **source_fields,
            }
        )
        for index in range(3):
            member = store.create_memory(
                {
                    "memory_key": f"launch-window-{index}-{uuid4().hex[:8]}",
                    "memory_type": "semantic",
                    "title": f"Launch window fact {index}",
                    "canonical_text": "The launch window moves to March after the review.",
                    "status": "active",
                    "value": {"text": "The launch window moves to March after the review."},
                    "domain": "personal",
                    "sensitivity": "internal",
                    "metadata_json": {"source_refs": [f"source:{source['id']}"]},
                }
            )
            store.update_memory_embedding(memory_id=str(member["id"]), vector=pad_embedding_vector([0.5, 0.1, 0.2]))
        artifact = VNextConsolidationService(store, embedding_provider=_OneVector()).generate_memory_consolidation(
            MemoryConsolidationRequest(agent_identity=None, sensitivity_allowed=list(ALLOWED_WITH_CONFIDENTIAL))
        )
    return artifact, source


def test_cluster_member_citing_a_confidential_health_source_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """The report copies the ``source_refs`` of each cluster member. The members are internal and the source they
    cite is confidential and in the health domain, so the report is labelled over the source and a key below
    confidential is refused it. The ref is still printed for a key that may read it."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="consolidation-cited-source-label@example.com")

    artifact, source = _consolidate_a_cluster_citing(
        database_url, user_id, source_fields={"domain": "health", "sensitivity": "confidential"}
    )
    trusted_key, admin_key = _keys(database_url, user_id)

    source_id = str(source["id"])
    assert artifact["metadata_json"]["consolidation"]["cluster_membership"], "the run must have a cluster"
    assert f"source:{source_id}" in artifact["metadata_json"]["source_refs"]

    # The read is the judge: the key below confidential is refused, and the id is nowhere in what it is told.
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    assert source_id not in json.dumps(body)
    assert (artifact["domain"], artifact["sensitivity"]) == ("health", "confidential")

    status, body = _get_artifact(str(artifact["id"]), user_id, admin_key)
    assert status == 200, body
    assert f"source:{source_id}" in body["metadata_json"]["source_refs"]


def test_cluster_member_citing_an_internal_source_leaves_the_report_readable_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """The control: the label follows the source, so a source no stricter than the members changes nothing."""
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="consolidation-cited-source-control@example.com")

    artifact, source = _consolidate_a_cluster_citing(database_url, user_id, source_fields={})
    trusted_key, _admin_key = _keys(database_url, user_id)

    assert artifact["metadata_json"]["consolidation"]["cluster_membership"]
    assert artifact["sensitivity"] == "internal"
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 200, body
    assert f"source:{source['id']}" in body["metadata_json"]["source_refs"]


# -- the refs a report copies can name a memory in more ways than ``memory:<id>`` ------------------------------------------

_CITED_WORDS = "Atlas played ZQXCONSOLIDATED for 115 hours"
# Every way a ref a member keeps can name a memory and carry words of it. ``memory:<id>`` alone is the one spelling the report
# already treated as an input.
_MEMORY_REFS = {
    "json quote": lambda m: json.dumps({"memory_id": m, "quote": _CITED_WORDS}),
    "json key": lambda m: json.dumps({"memory_id": m, f"{_CITED_WORDS} key words": None}),
    "sentence": lambda m: f"see memory {m}: {_CITED_WORDS}",
    "url": lambda m: f"https://example.test/memories/{m}?q={_CITED_WORDS.replace(' ', '+')}",
    "alice url and words": lambda m: f"alice://memories/{m} {_CITED_WORDS}",
}


def _consolidate_a_cluster_whose_refs_name_a_memory(
    database_url: str, user_id: UUID, *, memory_fields: dict, ref_for
) -> tuple[dict, dict]:
    """Three near-duplicate internal memories whose refs name one other memory in the spelling ``ref_for`` writes, clustered and
    reported on Postgres. Returns the stored report and the memory the refs name (the words in its text are ``_CITED_WORDS``)."""

    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        cited = store.create_memory(
            {
                "memory_key": f"cited-{uuid4().hex[:8]}",
                "memory_type": "episode",
                "title": "Atlas hours",
                "canonical_text": _CITED_WORDS,
                "status": "active",
                "value": {"text": _CITED_WORDS},
                "domain": "personal",
                "sensitivity": "public",
                "metadata_json": {},
                **memory_fields,
            }
        )
        for index in range(3):
            member = store.create_memory(
                {
                    "memory_key": f"launch-window-{index}-{uuid4().hex[:8]}",
                    "memory_type": "semantic",
                    "title": f"Launch window fact {index}",
                    "canonical_text": "The launch window moves to March after the review.",
                    "status": "active",
                    "value": {"text": "The launch window moves to March after the review."},
                    "domain": "personal",
                    "sensitivity": "internal",
                    "metadata_json": {"source_refs": [ref_for(str(cited["id"]))]},
                }
            )
            store.update_memory_embedding(memory_id=str(member["id"]), vector=pad_embedding_vector([0.5, 0.1, 0.2]))
        artifact = VNextConsolidationService(store, embedding_provider=_OneVector()).generate_memory_consolidation(
            MemoryConsolidationRequest(agent_identity=None, sensitivity_allowed=list(ALLOWED_WITH_CONFIDENTIAL))
        )
    return artifact, cited


@pytest.mark.parametrize("shape", list(_MEMORY_REFS))
def test_a_report_whose_member_refs_quote_a_confidential_memory_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch, shape: str
) -> None:
    """A member keeps its refs as it was given them, and the report copies them (and so do the candidates). A ref that names a
    memory some way other than ``memory:<id>`` (a JSON text that quotes it, a sentence, a URL) was not counted among the inputs of
    the report, so the label of the report did not cover the memory and a key below confidential read the words of a confidential
    memory in ``metadata_json.source_refs``. The report is labelled over the memories its refs name, as it is over the sources.

    Mutation: leave ``named_memories`` out of ``labelled_rows`` in ``generate_memory_consolidation``.
    """
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email=f"consolidation-cited-memory-label-{shape.replace(' ', '-')}@example.com")

    artifact, cited = _consolidate_a_cluster_whose_refs_name_a_memory(
        database_url, user_id, memory_fields={"sensitivity": "confidential"}, ref_for=_MEMORY_REFS[shape]
    )
    trusted_key, admin_key = _keys(database_url, user_id)

    assert artifact["metadata_json"]["consolidation"]["cluster_membership"], "the run must have a cluster"
    assert artifact["sensitivity"] == "confidential"
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    assert "ZQXCONSOLIDATED" not in json.dumps(body)

    status, body = _get_artifact(str(artifact["id"]), user_id, admin_key)
    assert status == 200, body
    assert "ZQXCONSOLIDATED" in json.dumps(body["metadata_json"]["source_refs"])


@pytest.mark.parametrize("shape", list(_MEMORY_REFS))
def test_a_report_whose_member_refs_quote_a_memory_that_is_redacted_afterwards_is_refused_to_a_trusted_agent(
    migrated_database_urls, monkeypatch, shape: str
) -> None:
    """The report records the memories its refs name as inputs, so redacting one of them contains the report with the rest of
    the rows made from it: a key with a ceiling is told there is no such artifact, and the list does not carry it. An unbound
    admin key still reads it.

    Mutation: leave ``named_memories`` out of the memories the report is derived from (``with_derived_from`` in
    ``generate_memory_consolidation``): the redacted memory no longer contains the report and the key reads the words.
    """
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email=f"consolidation-redacted-memory-{shape.replace(' ', '-')}@example.com")

    artifact, cited = _consolidate_a_cluster_whose_refs_name_a_memory(
        database_url, user_id, memory_fields={}, ref_for=_MEMORY_REFS[shape]
    )
    trusted_key, admin_key = _keys(database_url, user_id)
    assert artifact["sensitivity"] == "internal"
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 200, "readable before the memory is redacted"
    assert str(cited["id"]) in json.dumps(artifact["metadata_json"]["derived_from"]), "the memory the refs name is an input of the report"

    from alicebot_api.mcp.memories import redact_memory_flow

    with user_connection(database_url, user_id) as conn:
        redact_memory_flow(PostgresVNextStore(conn), memory_id=str(cited["id"]), reason="synthetic")

    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert (status, body) == (404, {"detail": "vNext artifact was not found"}), body
    status, listed = invoke_request(
        "GET", "/v0/vnext/artifacts", authorization=f"Bearer {trusted_key}", query_params={"user_id": str(user_id)}
    )
    assert status == 200 and "ZQXCONSOLIDATED" not in json.dumps(listed) and str(artifact["id"]) not in json.dumps(listed)
    status, body = _get_artifact(str(artifact["id"]), user_id, admin_key)
    assert status == 200, body
    assert "ZQXCONSOLIDATED" in json.dumps(body["metadata_json"]["source_refs"])


def test_refs_that_name_no_memory_or_only_the_members_leave_the_report_readable_to_a_trusted_agent(
    migrated_database_urls, monkeypatch
) -> None:
    """The control: a label such as ``meeting notes``, a URL with no id, and the ``memory:<id>`` of the members themselves add no
    input, so the report keeps the label of its members and a trusted key reads it.
    """
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="consolidation-refs-without-memories@example.com")

    artifact, cited = _consolidate_a_cluster_whose_refs_name_a_memory(
        database_url, user_id, memory_fields={"sensitivity": "confidential"}, ref_for=lambda _m: "meeting notes"
    )
    trusted_key, _admin_key = _keys(database_url, user_id)
    assert artifact["metadata_json"]["consolidation"]["cluster_membership"]
    assert artifact["sensitivity"] == "internal"
    status, body = _get_artifact(str(artifact["id"]), user_id, trusted_key)
    assert status == 200, body
    assert "meeting notes" in body["metadata_json"]["source_refs"]


def test_a_memory_the_refs_name_that_is_raised_between_two_runs_makes_a_new_report(migrated_database_urls, monkeypatch) -> None:
    """The report is stored once for the digest of its run, and a run returns the report it already made. The digest covers the
    memories the refs name beside the cluster, so a memory that is raised above the members between two runs makes a new report,
    labelled over it, and the first report is not returned for it.

    Mutation: leave ``named_memories`` out of the digest in ``generate_memory_consolidation`` (the second run returns the first
    report, which a trusted key can read and which names the memory it can no longer read).
    """
    database_url = migrated_database_urls["app"]
    _point_routes_at(monkeypatch, database_url)
    user_id = seed_user(database_url, email="consolidation-raised-memory-digest@example.com")

    first, cited = _consolidate_a_cluster_whose_refs_name_a_memory(
        database_url, user_id, memory_fields={}, ref_for=_MEMORY_REFS["json quote"]
    )
    assert first["sensitivity"] == "internal"
    from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock

    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        acquire_exclusive_label_lock(store)
        store.update_memory(memory_id=str(cited["id"]), patch={"sensitivity": "confidential"}, actor_type="system")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        second = VNextConsolidationService(store, embedding_provider=_OneVector()).generate_memory_consolidation(
            MemoryConsolidationRequest(agent_identity=None, sensitivity_allowed=list(ALLOWED_WITH_CONFIDENTIAL))
        )
    assert str(second["id"]) != str(first["id"]), "a memory the refs name changed, so the run is a new one"
    assert second["sensitivity"] == "confidential"
