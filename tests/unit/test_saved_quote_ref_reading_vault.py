"""The four defects an outside review found in the way a saved quote's refs are read, on a real vault with real keys.

Unreleased (on main, not in v0.20.0). ``tests/unit/test_saved_quote_ref_reading.py`` holds each case with a stub store. These
tests run them through the shipped doors and readers: refs are stored by the commit route and by the proposal route, a
source is reclassified, archived or removed with the handlers and statements a vault has, and every kind of key reads the
memory through ``alice_memory_review``, the MCP pack, the HTTP pack and ``alice_explain``. A unique quote is searched for in
the serialized answer, and a control reads each surface before the change so that an absent quote was withheld.

* A ref that merely contains a marker (``copied from source: <id>``, ``https://host/projects/1/sources/<id>``) names no source
  and must not change the answer of a caller who may read everything the memory cites.
* An id that names an archived source is judged wherever it is found, in a sentence, a URL or under any key.
* The metadata of a revision (a memory proposal keeps its ``source_refs`` there) is held to the fence like the memory's.
* A huge ref, which the proposal door stores as sent, is read in time that grows with its size.

Each test names the mutation that must fail it. The mutations were made by hand on a copy of the module and the file was
restored by copying the saved original back.
"""

from __future__ import annotations

import json
import time
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_saved_quote_every_copy import _commit_with_refs, _correct, _link_count, _two_sources
from tests.unit.test_saved_quotes_follow_the_source_fence import (
    _AGENT_IDS,
    _AUTHORIZED_AFTER,
    _KEY_SPECS,
    _USER_ID,
    _WORD_A,
    _Vault,
    _holds_quote,
    _readers,
    vault,  # noqa: F401  (a fixture of the sibling module)
)

# The ids a ref holds in the first two groups below name no source: they stand in for a chunk id or a session id.
_TIME_LIMIT_SECONDS = 2.0


def _surfaces(vault: _Vault, who: str | None, memory_id: str, query: str) -> dict[str, dict[str, object]]:
    """Every surface that carries a saved quote, for one caller."""

    return _readers(vault, who, memory_id, query)


def _propose(vault: _Vault, refs: list[object], *, tag: str) -> str:
    """``POST /v0/vnext/memory-proposals`` by the trusted key: the route stores ``source_refs`` as sent and bounds only the
    size of the request. Returns the id of the memory it made."""

    from alicebot_api.routers import vnext_memories as router

    response = router.create_vnext_memory_proposal(
        router.VNextMemoryProposalRequest(
            user_id=UUID(_USER_ID),
            title=f"Cone ten firing proposal {tag}",
            canonical_text=f"The cone ten firing proposal {tag} is posted on the wall calendar.",
            domain="project",
            sensitivity="internal",
            confidence=0.6,
            source_refs=refs,
            agent_id=_AGENT_IDS["trusted"],
        ),
        authorization=f"Bearer {vault.keys['trusted']}",
    )
    body = json.loads(response.body)
    assert response.status_code == 201, body
    return str(body["proposal"]["id"])


# -- 1. a ref that merely contains a marker --------------------------------------------------------------------------

_MARKER_SHAPES = {
    "outside URL": lambda a, b: [a, f"https://app.example.test/projects/1/sources/{b}"],
    "note with a source label": lambda a, b: [a, f"copied from source: {b}"],
    "label and id": lambda a, b: [a, f"source_id: {b}"],
    "outside URL after a word": lambda a, b: [a, f"see https://app.example.test/sources/{b}/chunks/3"],
}


@pytest.mark.parametrize("shape", list(_MARKER_SHAPES))
def test_a_ref_that_merely_contains_a_marker_changes_no_answer_for_a_caller_who_may_read_the_cited_source(
    vault: _Vault, shape: str
) -> None:
    """The case of the review. A commit with ``[A, "https://host/projects/1/sources/<id>"]`` or
    ``[A, "copied from source: <id>"]`` is accepted (the write fence reads neither), and the id names no source. The reader
    counted it as a named source that does not exist, so every key lost the quote from ``alice_memory_review``, the MCP pack
    and the HTTP pack, the admin key included, and ``alice_explain`` refused the memory; the owner was unaffected. Now every
    key gets the quote on every surface where the control found it, ``alice_explain`` answers, and the review is the
    owner's review byte for byte.

    Mutation: in ``_ids_in_text`` name an id that has ``source:`` or ``sources/`` just before it anywhere in the text (the
    rule of the previous version): every key loses the quote and explain refuses.
    """

    allowed, _unused = _two_sources(vault)
    stray = str(uuid4())
    memory_id, query, _body = _commit_with_refs(vault, _MARKER_SHAPES[shape](allowed, stray), tag="marker" + shape[:4])
    assert _link_count(vault, memory_id) == 1
    owner_review = vault.review(None, memory_id)["payload"]
    for who in _KEY_SPECS:
        surfaces = _surfaces(vault, who, memory_id, query)
        for surface, answer in surfaces.items():
            assert answer["is_error"] is False, (who, surface)
            assert _holds_quote(answer), (shape, who, surface)
        assert vault.explain(who, memory_id)["is_error"] is False, (shape, who)
        assert surfaces["review"]["payload"] == owner_review, (shape, who)


@pytest.mark.parametrize("variant", ("confidential", "archived"))
@pytest.mark.parametrize("shape", list(_MARKER_SHAPES))
def test_the_same_ref_withholds_the_quote_when_the_id_names_a_source_the_caller_may_not_read(
    vault: _Vault, shape: str, variant: str
) -> None:
    """The ids above are incidental, not ignored: when the id names a stored source (made confidential, or archived) the
    caller may not read, the quote is withheld from every key that may not read it and ``alice_explain`` refuses, and a key
    that may read it (the admin key, for a confidential source) keeps what it had.

    Mutation: leave the incidental ids out of ``refused`` in ``SavedProvenanceReader._refused``: every key keeps the quote.
    """

    allowed, restricted = _two_sources(vault)
    memory_id, query, _body = _commit_with_refs(
        vault, _MARKER_SHAPES[shape](allowed, restricted), tag="markerstored" + shape[:4]
    )
    for who in _KEY_SPECS:
        assert all(_holds_quote(answer) for answer in _surfaces(vault, who, memory_id, query).values()), ("control", who)
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _surfaces(vault, who, memory_id, query).items():
            assert _holds_quote(answer) is (who in authorized), (shape, variant, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (shape, variant, who)


# -- 2. an id that names an archived or removed source ---------------------------------------------------------------


def test_a_limit_an_id_that_names_a_source_removed_from_the_database_is_not_judged_where_it_could_be_a_chunk_id(
    vault: _Vault,
) -> None:
    """The stated limit that remains. An id under a key that does not hold a source reference (``origin``), in a sentence or
    in a URL is judged when the store holds a row for it, an archived source's row included. A source whose row is gone has
    no row to find, so its id cannot be told from a chunk id and the quote stays for a key that may read the other source.
    No door of the product removes a source row (sources are archived); the statement below is the only way to make one, and
    the same ref with the id under ``source_ids`` is refused (the named shapes of ``test_saved_quote_every_copy.py``).

    Mutation: none to make; this pins a limit, and it fails when the limit is closed.
    """

    allowed, restricted = _two_sources(vault)
    memory_id, _query, _body = _commit_with_refs(vault, [{"source_id": allowed, "origin": restricted}], tag="limitremoved")
    vault.sql("DELETE FROM sources WHERE id = ?", (restricted,))
    assert not vault.sql("SELECT id FROM sources WHERE id = ?", (restricted,))
    answer = vault.review("trusted", memory_id)
    assert answer["is_error"] is False
    assert _holds_quote(answer), "the stated limit: a removed source named under an unlisted key is not judged"
    assert vault.explain("trusted", memory_id)["is_error"] is False


def test_a_limit_an_incidental_id_tells_a_caller_who_stores_a_memory_whether_it_names_a_source_it_may_not_read(
    vault: _Vault,
) -> None:
    """The stated cost of judging an incidental id. A key that can commit stores ``{"source_id": A, "origin": X}`` (the write
    fence reads no ``origin``) and reads its own memory back: when ``X`` names a stored source the key may not read, the quote is
    withheld and ``alice_explain`` refuses; when ``X`` names nothing, neither happens. So one bit about an id the caller
    already holds is observable. It is the price of withholding the quote of a memory that names a confidential source under
    a free key, which would otherwise leak, without withholding the quote of every memory whose refs hold a chunk id. A named
    id (under ``source_ids``) answers alike for a missing, an archived and an unreadable source.

    Mutation: none to make; this pins the limit, and fails if a change makes the two answers equal (update the docs then).
    """

    allowed, restricted = _two_sources(vault)
    gone = vault.capture_source("Gamma log. Operator note: glaze batch twelve cooled overnight.", "Third log")
    unreadable_id, _query, _body = _commit_with_refs(
        vault, [{"source_id": allowed, "origin": restricted}], tag="oracleunreadable"
    )
    nothing_id, _query, _body = _commit_with_refs(
        vault, [{"source_id": allowed, "origin": str(uuid4())}], tag="oraclenothing"
    )
    named_unreadable_id, _query, _body = _commit_with_refs(
        vault, [{"source_ids": [allowed, restricted]}], tag="oraclenamedunreadable"
    )
    named_archived_id, _query, _body = _commit_with_refs(
        vault, [{"source_ids": [allowed, gone]}], tag="oraclenamedarchived"
    )
    named_missing_id = _propose(vault, [allowed, {"source_ids": [str(uuid4())]}], tag="oraclenamedmissing")
    vault.reclassify(restricted, "confidential")
    vault.reclassify(gone, "archived")
    # An incidental id: an unreadable stored source changes the answer, an id that names nothing does not.
    assert _holds_quote(vault.review("trusted", nothing_id)) and vault.explain("trusted", nothing_id)["is_error"] is False
    assert not _holds_quote(vault.review("trusted", unreadable_id))
    assert vault.explain("trusted", unreadable_id)["is_error"] is True
    # A named id: an unreadable, an archived and a missing source are refused the same way.
    for memory_id in (named_unreadable_id, named_archived_id, named_missing_id):
        assert not _holds_quote(vault.review("trusted", memory_id)), memory_id
        assert vault.explain("trusted", memory_id)["is_error"] is True, memory_id


def test_the_store_lookup_that_includes_archived_sources_returns_them_and_the_usual_lookup_does_not(vault: _Vault) -> None:
    """``get_sources_by_ids(..., include_deleted=True)`` is what lets the reader tell an id that names an archived source from
    an id that names none. On the SQLite store of a real vault: an archived source's row comes back with ``deleted_at`` set, a
    live source's row with none, an id that names nothing is absent, another user's lookup of the same ids finds nothing, an
    empty list reads nothing, and the plain ``get_sources_by_ids`` still leaves the archived source out. (``tests/integration/
    test_saved_quotes_postgres.py`` runs the same on Postgres in CI.)

    Mutations, each alone, in ``SQLiteVNextStore.get_sources_by_ids``: make ``live_only`` always ``AND deleted_at IS NULL``
    (the archived row is gone); drop ``user_id = ?`` (the other user finds the rows).
    """

    allowed, restricted = _two_sources(vault)
    vault.reclassify(restricted, "archived")
    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        assert {str(row["id"]) for row in store.get_sources_by_ids([allowed, restricted])} == {allowed}
        rows = {
            str(row["id"]): row
            for row in store.get_sources_by_ids([allowed, restricted, str(uuid4()), allowed], include_deleted=True)
        }
        assert set(rows) == {allowed, restricted}
        assert rows[allowed]["deleted_at"] is None and rows[restricted]["deleted_at"] is not None
        assert store.get_sources_by_ids([], include_deleted=True) == []
        assert SQLiteVNextStore(conn, str(uuid4())).get_sources_by_ids([allowed, restricted], include_deleted=True) == []


# -- 3. the metadata of a revision -----------------------------------------------------------------------------------


def test_the_refs_a_proposal_keeps_in_its_revision_follow_the_fence_like_the_memorys_own(vault: _Vault) -> None:
    """A memory proposal that cites a source it may read (A) and one (B) that is later made confidential is stored with its
    ``source_refs`` in the memory and in ``metadata_json`` of its first revision. After B is made confidential,
    ``alice_memory_review`` by id of the memory, read by a key that may not read B, carried B's id at
    ``review.revisions[0].metadata_json.source_refs`` (an id and no quote, but a sign that B exists, which
    ``alice_explain`` refuses for the same key). Now the entry is dropped there as it is from the memory, A stays, and the
    admin key, which may read B, still sees both. Before the change every key sees both ids.

    Mutations: drop the ``metadata_json`` read from ``_source_ids_named_by_revision``, or the ``metadata_json`` scrub from
    ``_revision_without_refused_refs`` (``vnext_source_fence.py``): the id stays in the revision.
    """

    allowed, restricted = _two_sources(vault)
    memory_id = _propose(vault, [allowed, restricted], tag="revisionrefs")

    def revision_refs(who: str) -> list[object]:
        answer = vault.review(who, memory_id)
        assert answer["is_error"] is False, (who, answer["text"][:300])  # type: ignore[index]
        revisions = answer["payload"]["review"]["revisions"]  # type: ignore[index]
        refs = revisions[0]["metadata_json"]["source_refs"]
        return refs if isinstance(refs, list) else [refs]

    for who in _KEY_SPECS:
        assert restricted in json.dumps(revision_refs(who)), ("control", who)
    vault.reclassify(restricted, "confidential")
    for who in _KEY_SPECS:
        shown = json.dumps(revision_refs(who))
        if who in _AUTHORIZED_AFTER["confidential"]:
            assert restricted in shown and allowed in shown, who
        else:
            assert restricted not in shown and allowed in shown, who
            assert restricted not in json.dumps(vault.review(who, memory_id)["payload"]), who
            assert vault.explain(who, memory_id)["is_error"] is True, who


# -- 4. a huge ref ---------------------------------------------------------------------------------------------------


def test_a_ref_of_a_hundred_kilobytes_stored_by_the_proposal_door_is_read_in_time_that_grows_with_its_size(
    vault: _Vault,
) -> None:
    """The proposal route stores ``source_refs`` as sent. One proposal with a single 150 KB ref string (4,000 ids) made
    ``alice_memory_review`` of the memory take 12 s and ``alice_explain`` 5 s for every key bound to the project, against
    0.03 s and 0.02 s before the change; the owner was unaffected because the reader returns early for the owner. Both
    calls now stay under the limit for every key.

    Mutation: add ``_REFLOW.search(text, 0, start)`` with ``_REFLOW = re.compile(r"memory:$", re.IGNORECASE)`` to the loop of
    ``_ids_in_text`` (``vnext_source_fence.py``), which rescans the text before each id: both calls take seconds and fail.
    """

    allowed, _unused = _two_sources(vault)
    huge = ",".join(str(uuid4()) for _ in range(4_000))
    memory_id = _propose(vault, [allowed, huge], tag="hugeref")
    for who in ("project", "admin"):
        started = time.perf_counter()
        review = vault.review(who, memory_id)
        review_seconds = time.perf_counter() - started
        started = time.perf_counter()
        vault.explain(who, memory_id)
        explain_seconds = time.perf_counter() - started
        assert review["is_error"] is False, who
        assert review_seconds < _TIME_LIMIT_SECONDS, (who, f"review {review_seconds:.2f} s")
        assert explain_seconds < _TIME_LIMIT_SECONDS, (who, f"explain {explain_seconds:.2f} s")


# -- 5. what a review stores when no quote is sent -------------------------------------------------------------------


def test_a_review_that_sends_no_quote_stores_the_memory_text_on_the_link_and_null_in_the_copy(vault: _Vault) -> None:
    """The producer fact the module docstring relies on, run for real. An edit-and-approve review and a supersede review that
    name a source and send no quote store the memory's own text as the quote of the link and ``null`` as the quote of the
    copy in the memory's metadata, so there is no second copy of any source's text to match, and the text that is on the link
    is the text the memory returns anyway.

    Mutation: replace ``or str(updated.get("canonical_text") or "")`` in the link of the edit-and-approve branch of
    ``mcp/review.py`` with ``or None`` (and the same for the supersede branch with ``or canonical_text``): the link holds no
    quote and the assertions on the link fail.
    """

    allowed, _unused = _two_sources(vault)
    candidate = vault._candidate("Noquotereview: the alpha kiln is fired on Mondays")
    _correct(
        vault,
        review_item_id=candidate,
        action="edit-and-approve",
        body={"text": "Kiln schedule: the alpha kiln is fired on Mondays."},
        provenance={"source_id": allowed},
    )
    row = vault.sql("SELECT canonical_text, metadata_json FROM memories WHERE id = ?", (candidate,))[0]
    link = vault.sql("SELECT quote FROM provenance_links WHERE target_id = ? AND evidence_role = 'supports'", (candidate,))
    assert [item["quote"] for item in link] == [row["canonical_text"]]
    assert json.loads(row["metadata_json"])["provenance"]["quote"] is None

    candidate = vault._candidate("Noquotesupersede: the alpha glaze shelf was reorganised on Friday")
    done = _correct(
        vault,
        review_item_id=candidate,
        action="supersede-existing",
        replacement_title="Alpha glaze shelf v2",
        replacement_body={"text": "The alpha glaze shelf was reorganised on Saturday morning."},
        replacement_provenance={"source_id": allowed},
        reason="moved",
    )
    replacement = str(done["payload"]["replacement_object"]["id"])  # type: ignore[index]
    row = vault.sql("SELECT canonical_text, metadata_json FROM memories WHERE id = ?", (replacement,))[0]
    link = vault.sql("SELECT quote FROM provenance_links WHERE target_id = ?", (replacement,))
    assert [item["quote"] for item in link] == [row["canonical_text"]]
    assert json.loads(row["metadata_json"])["replacement_provenance"]["quote"] is None
