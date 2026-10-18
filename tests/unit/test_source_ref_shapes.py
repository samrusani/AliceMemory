"""Write doors accept the string shapes producers store and refuse anything else.

Producers write ``source:<id>``, ``memory:<id>``, a bare id, or a label such as a URL. They do not write an object.
The commit service, the proposal, and the agent-output ingest refuse an object. The HTTP models are ``list[str]``,
so an object is HTTP 422. A backup that already holds an object restores, and the reader still sees it.

Mutation: drop the string check in ``_object_tuple``. ``test_the_commit_service_refuses_an_object_ref`` fails.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.routers.vnext_memories import (
    VNextAgentOutputIngestRequest,
    VNextMemoryCommitRequest,
    VNextMemoryProposalRequest,
)
from alicebot_api.vnext_connectors import VNextConnectorService, VNextConnectorValidationError
from alicebot_api.vnext_memory_commit import VNextMemoryCommitValidationError, _object_tuple
from alicebot_api.vnext_memory_propose import MemoryProposal, MemoryProposalInvalid
from tests.unit.fixtures_backup_import import origin_export
from tests.unit.test_sqlite_onramp import USER_ID as SEED_USER_ID


SHAPES = ("source:" + str(uuid4()), "memory:" + str(uuid4()), str(uuid4()), "https://example.test/kiln")


def test_the_commit_service_accepts_every_producer_string_and_refuses_the_rest() -> None:
    assert _object_tuple(list(SHAPES)) == SHAPES
    for bad in ({"source_id": str(uuid4()), "quote": "x"}, 3, True, None, ["nested"]):
        with pytest.raises(VNextMemoryCommitValidationError, match="source_refs entries must be strings"):
            _object_tuple([bad])


def test_the_proposal_and_the_ingest_refuse_an_object() -> None:
    with pytest.raises(MemoryProposalInvalid, match="source_refs entries must be strings"):
        MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=({"quote": "x"},))
    MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=SHAPES)

    class _Store:
        pass

    with pytest.raises(VNextConnectorValidationError, match="source_refs entries must be strings"):
        VNextConnectorService(_Store()).ingest_agent_output({"title": "t", "content": "c", "source_refs": [{"quote": "x"}]})


def test_the_http_models_are_422_for_an_object() -> None:
    from pydantic import ValidationError

    body = {"user_id": str(uuid4()), "title": "t", "canonical_text": "c", "source_refs": [{"quote": "x"}]}
    for model in (VNextMemoryCommitRequest, VNextMemoryProposalRequest):
        with pytest.raises(ValidationError):
            model.model_validate(body)
    ingest = {
        "user_id": str(uuid4()),
        "agent_id": "a",
        "title": "t",
        "content": "c",
        "source_refs": [{"quote": "x"}],
    }
    with pytest.raises(ValidationError):
        VNextAgentOutputIngestRequest.model_validate(ingest)
    accepted = {**body, "source_refs": list(SHAPES)}
    assert VNextMemoryCommitRequest.model_validate(accepted).source_refs == list(SHAPES)


def test_a_backup_that_holds_an_object_ref_restores(tmp_path) -> None:
    export = origin_export(tmp_path)
    memories = [item for item in export.body if item.get("record_type") == "memory"]
    assert memories, sorted({item["record_type"] for item in export.body})
    record = memories[0]["record"]
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    metadata["source_refs"] = [{"memory_id": str(uuid4()), "quote": "kept from the old vault"}]
    record["metadata_json"] = metadata
    backup = export.write(tmp_path / "backup.jsonl")
    database = tmp_path / "restored.db"
    from alicebot_api import onramp as onramp_module

    bootstrap_database(database, user_id=SEED_USER_ID, user_email="local@alice")
    assert onramp_module.main(["import", "--in", str(backup), "--db", str(database), "--user-id", str(SEED_USER_ID)]) == 0
    row = sqlite3.connect(database).execute(
        "SELECT metadata_json FROM memories WHERE id = ?", (record["id"],)
    ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored["source_refs"][0]["quote"] == "kept from the old vault"
