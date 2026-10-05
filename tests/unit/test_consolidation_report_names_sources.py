"""A consolidation report is labelled over the sources its refs name.

The report copies ``metadata_json.source_refs`` of each proposed cluster member as stored (``source:<id>`` and
whatever else a writer put there), and so do the candidate memories. The refs stay: they are the provenance. But
``GET /v0/vnext/artifacts/{id}`` decides by the stored domain and sensitivity alone, and a member can be less sensitive
than a source it cites (the source was reclassified, or the member was written by a key that could read it). So the
report's label is taken over those sources as well, and its run digest covers them, so a source that was reclassified
makes a new report instead of returning the earlier one.

The printed refs are the judge: each test reads them back from the report and checks the stored label against the
rows they name.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

import alicebot_api.vnext_consolidation as consolidation_module
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService
from tests.unit.test_vnext_consolidation import (
    FakeConsolidationStore,
    MappedEmbeddingProvider,
    _seed_memory,
    _seed_six_memories,
)

EVERYTHING = list(ALL_SENSITIVITY)


class SourceReadingStore(FakeConsolidationStore):
    """The consolidation fake with the by-id source read both stores have, and the exact report lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[dict] = []
        self.source_reads: list[tuple[tuple[str, ...], bool]] = []

    def get_sources_by_ids(self, ids, *, include_deleted: bool = False):
        self.source_reads.append((tuple(str(value) for value in ids), include_deleted))
        wanted = {str(value).lower() for value in ids}
        return [
            dict(row)
            for row in self.sources
            if str(row["id"]).lower() in wanted and (include_deleted or row.get("deleted_at") is None)
        ]

    def find_artifact_by_workflow_digest(self, *, artifact_type, workflow, digest, scope_projects=None):
        for row in self.artifacts:
            metadata = row.get("metadata_json")
            if (
                row.get("artifact_type") == artifact_type
                and isinstance(metadata, dict)
                and metadata.get("workflow") == workflow
                and metadata.get("consolidation_digest") == digest
            ):
                return dict(row)
        return None


class SingleSourceStore(FakeConsolidationStore):
    """A store that can read one source by id and has no bulk read."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[dict] = []
        self.single_reads: list[str] = []

    def get_source(self, source_id: str):
        self.single_reads.append(str(source_id))
        for row in self.sources:
            if str(row["id"]).lower() == str(source_id).lower():
                return dict(row)
        return None


def _source(store, **fields) -> dict:
    row = {
        "id": str(uuid4()),
        "source_type": "manual_text",
        "title": "A source",
        "domain": "personal",
        "sensitivity": "internal",
        "metadata_json": {},
        **fields,
    }
    store.sources.append(row)
    return row


def _run(store, mapping, **request) -> dict:
    request.setdefault("sensitivity_allowed", EVERYTHING)
    return VNextConsolidationService(
        store, embedding_provider=MappedEmbeddingProvider(mapping)
    ).generate_memory_consolidation(MemoryConsolidationRequest(**request))


def _cluster_citing(store, refs_of_members) -> tuple[dict[str, list[float]], list[dict]]:
    """Seed the near-duplicate trio and the three distinct memories. Each member of the trio cites the refs
    ``refs_of_members(index)`` returns (as stored, one list per member)."""

    mapping: dict[str, list[float]] = {}
    near_dups, _distinct = _seed_six_memories(store, mapping)
    by_id = {str(row["id"]): row for row in store.memories}
    for index, member in enumerate(near_dups):
        by_id[str(member["id"])]["metadata_json"]["source_refs"] = list(refs_of_members(index))
    return mapping, near_dups


def _members_label(store, near_dups, **patch) -> None:
    ids = {str(row["id"]) for row in near_dups}
    for row in store.memories:
        if str(row["id"]) in ids:
            row.update(patch)


def _candidates(store) -> list[dict]:
    return [
        row
        for row in store.memories
        if row.get("status") == "candidate"
        and isinstance(row.get("metadata_json"), dict)
        and row["metadata_json"].get("candidate_kind") == "memory_consolidation"
    ]


# -- the label covers the sources the printed refs name -----------------------------------


@pytest.mark.parametrize("source_sensitivity", ("private", "confidential", "highly_sensitive", "sacred", "regulated"))
def test_a_member_citing_a_restricted_source_labels_the_report_and_the_refs_stay(source_sensitivity: str) -> None:
    store = SourceReadingStore()
    source = _source(store, sensitivity=source_sensitivity)
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    artifact = _run(store, mapping)

    assert artifact["metadata_json"]["consolidation"]["cluster_membership"], "the run must have a cluster"
    assert f"source:{source['id']}" in artifact["metadata_json"]["source_refs"], "the ref is provenance and stays"
    (candidate,) = _candidates(store)
    assert f"source:{source['id']}" in candidate["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == source_sensitivity


def test_a_member_citing_a_source_in_a_restricted_domain_labels_the_domain_of_the_report() -> None:
    store = SourceReadingStore()
    source = _source(store, domain="health", sensitivity="internal")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    artifact = _run(store, mapping)

    assert f"source:{source['id']}" in artifact["metadata_json"]["source_refs"]
    assert artifact["domain"] == "health"
    assert artifact["sensitivity"] == "internal"


def test_a_cited_source_below_the_members_does_not_lower_the_report() -> None:
    store = SourceReadingStore()
    source = _source(store, domain="personal", sensitivity="public")
    mapping, near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    _members_label(store, near_dups, sensitivity="private", domain="health")
    artifact = _run(store, mapping)

    assert artifact["sensitivity"] == "private"
    assert artifact["domain"] == "health"


def test_a_source_cited_by_every_member_counts_once_for_the_domain() -> None:
    """Three legal members cite one health source. The domain is the most frequent restricted label among the
    distinct rows, so it is legal (three rows against one). A source counted once for each ref that names it would be
    three health rows against three legal ones, and health sorts first."""

    store = SourceReadingStore()
    source = _source(store, domain="health")
    mapping, near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    _members_label(store, near_dups, domain="legal")
    artifact = _run(store, mapping)

    assert artifact["domain"] == "legal"


@pytest.mark.parametrize(
    "spelling",
    (
        "source:{id}",
        "SOURCE:{ID}",
        "{id}",
        "{hex}",
        "{{{id}}}",
        "urn:uuid:{id}",
        "alice://sources/{id}",
        "https://example.test/sources/{id}",
        "copied from source: {id}",
        '{{"source_id": "{id}"}}',
    ),
)
def test_a_ref_in_any_spelling_the_report_prints_labels_the_report(spelling: str) -> None:
    """The report copies the string as stored, so a reader sees the id in the spelling it was written in."""

    store = SourceReadingStore()
    source = _source(store, sensitivity="confidential")
    ref = spelling.format(id=source["id"], ID=str(source["id"]).upper(), hex=str(source["id"]).replace("-", ""))
    mapping, _near_dups = _cluster_citing(store, lambda _index: [ref])
    artifact = _run(store, mapping)

    assert ref in artifact["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == "confidential", spelling


@pytest.mark.parametrize("sensitivity", ("private", "confidential", "highly_sensitive"))
def test_a_source_id_that_reaches_the_printed_report_has_its_source_label_behind_it(sensitivity: str) -> None:
    """The printed report is the judge, as in the sweep: the id of each seeded source is looked for in the content and
    the metadata of the report, in the spellings a reader would match, and wherever it is found the stored label has
    to be that source's label or stricter. The ids are placed where a member can carry one: a ref, a ref in text, a
    ref that is JSON text, a ``source_id`` key and a key the report never copies."""

    rank = {"public": 1, "internal": 2, "unknown": 2, "private": 3, "confidential": 4, "highly_sensitive": 5}
    store = SourceReadingStore()
    cited = _source(store, sensitivity=sensitivity)
    keyed = _source(store, sensitivity=sensitivity)
    other = _source(store, sensitivity=sensitivity)
    mapping, near_dups = _cluster_citing(
        store,
        lambda index: [
            f"source:{cited['id']}",
            f"see https://example.test/sources/{cited['id']}",
            json.dumps({"id": str(cited["id"])}),
        ][index : index + 1],
    )
    by_id = {str(row["id"]): row for row in store.memories}
    for member in near_dups:
        by_id[str(member["id"])]["metadata_json"]["source_id"] = str(keyed["id"])
        by_id[str(member["id"])]["metadata_json"]["origin"] = f"imported from {other['id']}"
    artifact = _run(store, mapping)

    printed = (artifact["content_markdown"] + json.dumps(artifact["metadata_json"], default=str)).casefold()
    seen = [row for row in (cited, keyed, other) if str(row["id"]).casefold() in printed]
    assert cited in seen, "the cited source must reach the report, or the test proves nothing"
    for row in seen:
        assert rank[artifact["sensitivity"]] >= rank[str(row["sensitivity"])], row["id"]


def test_a_ref_that_names_no_stored_source_is_printed_and_adds_nothing() -> None:
    store = SourceReadingStore()
    missing = str(uuid4())
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{missing}", "https://example.test/notes"])
    artifact = _run(store, mapping)

    assert f"source:{missing}" in artifact["metadata_json"]["source_refs"]
    assert "https://example.test/notes" in artifact["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == "internal"


def test_a_ref_that_names_a_memory_is_not_read_as_a_source() -> None:
    """``memory:<id>`` names a memory, and the members' own labels already cover the memories of the cluster."""

    store = SourceReadingStore()
    mapping, near_dups = _cluster_citing(store, lambda _index: [])
    artifact = _run(store, mapping)

    assert any(ref.startswith("memory:") for ref in artifact["metadata_json"]["source_refs"])
    assert store.source_reads == [], "a run that cites no source reads no source"
    assert artifact["sensitivity"] == "internal"


def test_an_archived_source_still_labels_the_report() -> None:
    store = SourceReadingStore()
    source = _source(store, sensitivity="confidential", deleted_at="2026-09-01T00:00:00Z")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    artifact = _run(store, mapping)

    assert f"source:{source['id']}" in artifact["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == "confidential"
    assert all(include_deleted for _ids, include_deleted in store.source_reads)


def test_only_the_sources_the_report_prints_are_read() -> None:
    """A distinct memory that is in no cluster cites a confidential source. The report prints no ref of it, so the
    label leaves it out and the store is never asked for it."""

    store = SourceReadingStore()
    cited = _source(store, sensitivity="private")
    unprinted = _source(store, sensitivity="confidential")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{cited['id']}"])
    stray = next(row for row in store.memories if row["title"] == "Distinct fact 0")
    stray["metadata_json"]["source_refs"] = [f"source:{unprinted['id']}"]
    artifact = _run(store, mapping)

    assert unprinted["id"] not in json.dumps(artifact["metadata_json"], default=str)
    assert artifact["sensitivity"] == "private"
    assert store.source_reads == [((str(cited["id"]),), True)]


def test_the_sources_of_a_run_are_read_in_one_batch() -> None:
    store = SourceReadingStore()
    sources = [_source(store, sensitivity="internal") for _ in range(3)]
    mapping, _near_dups = _cluster_citing(
        store, lambda index: [f"source:{sources[index]['id']}", f"source:{sources[0]['id']}"]
    )
    _run(store, mapping)

    assert len(store.source_reads) == 1
    assert store.source_reads[0][0] == tuple(sorted(str(row["id"]) for row in sources))


def test_a_store_that_cannot_read_sources_prints_the_refs_and_labels_by_the_members() -> None:
    store = FakeConsolidationStore()
    cited = str(uuid4())
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{cited}"])
    artifact = _run(store, mapping)

    assert f"source:{cited}" in artifact["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == "internal"


def test_a_store_that_reads_one_source_at_a_time_labels_the_report_over_it() -> None:
    store = SingleSourceStore()
    source = _source(store, sensitivity="confidential", domain="health")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    artifact = _run(store, mapping)

    assert store.single_reads == [str(source["id"])]
    assert f"source:{source['id']}" in artifact["metadata_json"]["source_refs"]
    assert artifact["sensitivity"] == "confidential"
    assert artifact["domain"] == "health"


# -- the run digest covers the sources it names ------------------------------------------


def _digest_inputs_of_each_run(monkeypatch) -> list[dict]:
    """The payloads the service hashed into a run digest, in order (the one with ``corpus_digest`` is the run's)."""

    seen: list[dict] = []
    real = consolidation_module._digest_payload

    def spy(payload):
        if isinstance(payload, dict) and "corpus_digest" in payload:
            seen.append(payload)
        return real(payload)

    monkeypatch.setattr(consolidation_module, "_digest_payload", spy)
    return seen


def test_the_same_inputs_return_the_report_an_earlier_run_made() -> None:
    store = SourceReadingStore()
    source = _source(store, sensitivity="confidential")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    first = _run(store, mapping)
    second = _run(store, mapping)

    assert second["id"] == first["id"]
    assert len([row for row in store.artifacts if row["artifact_type"] == "memory_consolidation"]) == 1


def test_reclassifying_a_cited_source_makes_a_new_report() -> None:
    store = SourceReadingStore()
    source = _source(store, sensitivity="internal")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    first = _run(store, mapping)
    assert first["sensitivity"] == "internal"
    source["sensitivity"] = "confidential"
    second = _run(store, mapping)

    assert second["id"] != first["id"]
    assert second["metadata_json"]["consolidation_digest"] != first["metadata_json"]["consolidation_digest"]
    assert second["sensitivity"] == "confidential"


def test_moving_a_cited_source_to_a_restricted_domain_makes_a_new_report() -> None:
    """Only the domain of the source changes. A digest over the sensitivity alone would return the earlier report,
    and with it the earlier domain."""

    store = SourceReadingStore()
    source = _source(store, domain="personal", sensitivity="internal")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    first = _run(store, mapping)
    assert first["domain"] != "health"
    source["domain"] = "health"
    second = _run(store, mapping)

    assert second["id"] != first["id"]
    assert second["domain"] == "health"


def test_a_run_that_names_no_source_keeps_the_digest_it_always_had(monkeypatch) -> None:
    """With no source named the payload that is hashed has no ``named_sources`` key, so the digest is the one a run
    had before the key existed, and the report an earlier run made is still found."""

    seen = _digest_inputs_of_each_run(monkeypatch)
    store = SourceReadingStore()
    mapping, _near_dups = _cluster_citing(store, lambda _index: [])
    first = _run(store, mapping)
    second = _run(store, mapping)

    assert seen and all("named_sources" not in payload for payload in seen)
    assert second["id"] == first["id"]


def test_a_run_that_names_a_source_hashes_its_id_domain_and_sensitivity(monkeypatch) -> None:
    seen = _digest_inputs_of_each_run(monkeypatch)
    store = SourceReadingStore()
    source = _source(store, domain="health", sensitivity="confidential")
    mapping, _near_dups = _cluster_citing(store, lambda _index: [f"source:{source['id']}"])
    _run(store, mapping)

    assert seen[0]["named_sources"] == [{"id": str(source["id"]), "domain": "health", "sensitivity": "confidential"}]


@pytest.mark.parametrize("shared_id", [False, True], ids=["distinct-ids", "source-shares-the-memory-id"])
def test_a_source_that_shares_a_memory_id_never_stands_in_for_the_memory_label(tmp_path, shared_id):
    """An id is unique only within its own table, so a public source may carry the id of a confidential memory. The
    report prints the memory's text and names the source; its label is taken over both, so it stays confidential and
    a trusted_local_agent key is refused, whether or not the two ids are equal. (Found by a review of #561 on real
    SQLite stores.)

    Mutation: count memories and sources in one map keyed by id (``labelled_rows`` built from one ``_one_row_per_id``
    over the members, the roll-up rows and ``named_sources`` together: the source replaces the memory and the report
    is internal).
    """

    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.routers._vnext_shared import _vnext_exact_resource_policy
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_control import AgentIdentity
    from tests.unit.test_vnext_consolidation import MappedEmbeddingProvider, _seed_six_memories

    user = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = tmp_path / "shared-id.sqlite3"
    bootstrap_database(path, user_id=user, user_email="probe@local")
    with sqlite_user_connection(path, user) as conn:
        store = SQLiteVNextStore(conn, user)
        # SQLite has no artifact table; keep the payload the producer writes, label and all.
        store.create_artifact = lambda payload, **kwargs: {"id": str(uuid4()), **payload}
        mapping = {}
        members, _ = _seed_six_memories(store, mapping)
        protected = members[0]
        memory_id = str(protected["id"])
        source_id = memory_id if shared_id else str(uuid4())
        store.create_source({"id": source_id, "source_type": "manual_text", "content_hash": "public-source",
                             "title": "Public source", "domain": "project", "sensitivity": "public"})
        store.update_memory(memory_id=memory_id, patch={
            "sensitivity": "confidential", "domain": "project",
            "metadata_json": {"source_refs": [f"source:{source_id}"]},
        })
        report = VNextConsolidationService(store, embedding_provider=MappedEmbeddingProvider(mapping)).generate_memory_consolidation(
            MemoryConsolidationRequest(sensitivity_allowed=list(ALL_SENSITIVITY), propose_rollups=False,
                                       create_candidate_memories=False))
    assert source_id in json.dumps(report["metadata_json"]) and protected["canonical_text"] in report["content_markdown"]
    assert (report["domain"], report["sensitivity"]) == ("project", "confidential")
    access = _vnext_exact_resource_policy(
        identity=AgentIdentity(agent_id="trusted-reader", permission_profile="trusted_local_agent", auth="api_key"),
        action="artifact.lookup", resource=report)
    assert access.decision == "blocked"
