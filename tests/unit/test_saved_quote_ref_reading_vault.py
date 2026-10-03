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
from tests.unit.test_saved_quote_every_copy import _ALLOWED_TEXT, _commit_with_refs, _correct, _link_count, _two_sources
from tests.unit.test_saved_quote_ref_reading import _zero_led_id
from tests.unit.test_saved_quotes_follow_the_source_fence import (
    _AGENT_IDS,
    _AUTHORIZED_AFTER,
    _KEY_SPECS,
    _USER_ID,
    _Vault,
    _holds_quote,
    _readers,
    vault,  # noqa: F401  (a fixture of the sibling module)
)

# A linear reader takes a few hundredths of a second on the huge ref below and a quadratic one takes seconds.
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
        started = time.process_time()
        review = vault.review(who, memory_id)
        review_seconds = time.process_time() - started
        started = time.process_time()
        vault.explain(who, memory_id)
        explain_seconds = time.process_time() - started
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


# -- 6. a source whose id starts with 0, named with whitespace in the place of the 0 -----------------------------------


def _two_sources_with_a_zero_led_b(vault: _Vault, zeros: int) -> tuple[str, str]:
    """The source the keys may keep reading (``a``) and the one that is reclassified later (``b``), whose id starts with
    ``zeros`` zeros. Both are captured through ``alice_capture``; the store is given ``b``'s id for that one capture."""

    wanted = _zero_led_id(zeros)
    allowed = vault.capture_source(_ALLOWED_TEXT, "Second log")
    original = SQLiteVNextStore.get_or_create_source

    def with_the_wanted_id(self: SQLiteVNextStore, source: dict[str, object], **kwargs: object):  # type: ignore[no-untyped-def]
        return original(self, {**source, "id": wanted}, **kwargs)  # type: ignore[arg-type]

    with vault.monkeypatch.context() as patch:
        patch.setattr(SQLiteVNextStore, "get_or_create_source", with_the_wanted_id)
        restricted = vault.capture_source()
    assert restricted == wanted
    return allowed, restricted


_ZERO_LED_SHAPES = {
    "source: and a space": lambda a, b: [{"source_ids": [a, "source: " + b.replace("-", "")[1:]]}],
    "source: and a tab, hyphens kept": lambda a, b: [{"source_ids": [a, "source:\t" + b[1:]]}],
    "a space under an id key": lambda a, b: [{"source_id": a, "id": " " + b.replace("-", "")[1:]}],
    "a newline after the id, stripped first by the writer": lambda a, b: [
        {"source_ids": [a, "source: " + b.replace("-", "")[1:] + "\n"]}
    ],
}
_ZERO_LED_DOORS = ("http_commit", "held", "confirm")


def _commit_through(vault: _Vault, door: str, refs: list[object], *, tag: str) -> tuple[str, str]:
    """``POST /v0/vnext/memories/commit`` with the refs as given, then the step that makes the memory active: nothing for a
    commit accepted at once (a link to the first id of each ref, and the excerpt as its quote and as the memory's own copy);
    an approval for a commit held for review (confidence under 0.5); a confirmation for one that waits for its author
    (confidence under 0.85). The last two store no link at all."""

    if door == "http_commit":
        memory_id, query, _body = _commit_with_refs(vault, refs, tag=tag)
        return memory_id, query
    confidence = 0.4 if door == "held" else 0.7
    memory_id, query, body = _commit_with_refs(vault, refs, tag=tag, confidence=confidence)
    assert body["status"] == ("review_required" if door == "held" else "confirmation_required"), body
    assert _link_count(vault, memory_id) == 0
    if door == "held":
        done = vault.wire(
            "alice_memory_correct", {"review_item_id": memory_id, "action": "approve", "reason": "check"}, who=vault.reviewer
        )
    else:
        done = vault.wire(
            "alice_memory_commit",
            {"confirmation_id": body["confirmation"]["confirmation_id"], "confirmation_action": "confirm"},
            who="trusted",
        )
    assert done["is_error"] is False, done
    assert _link_count(vault, memory_id) == 0, "approving or confirming adds no link"
    return memory_id, query


def _zero_led_params() -> list[object]:
    """Every shape through the door that links, the two that matter through the doors that store no link, and the archived
    and the three zeros variants through each door."""

    cases: list[tuple[str, str, int, str]] = []
    for door in _ZERO_LED_DOORS:
        shapes = list(_ZERO_LED_SHAPES) if door == "http_commit" else ["source: and a space", "a newline after the id, stripped first by the writer"]
        cases += [(door, shape, 1, "confidential") for shape in shapes]
        cases += [(door, "source: and a space", 1, "archived"), (door, "source: and a space", 3, "confidential")]
    return [pytest.param(*case, id="-".join(map(str, case))) for case in cases]


@pytest.mark.parametrize(("door", "shape", "zeros", "variant"), _zero_led_params())
def test_a_source_with_a_leading_zero_named_with_whitespace_keeps_its_quote_from_every_reader(
    vault: _Vault, door: str, shape: str, zeros: int, variant: str
) -> None:
    """The case of the outside review of the second head. A source whose id starts with ``0`` can be named by 31 digits and a
    whitespace character (``source: `` and the digits is the form of the review): ``uuid.UUID`` reads it as the id with its
    zero, so the commit route links and fences the source, and the reader, which stripped the string first, named nothing.
    B was then made confidential or archived and the excerpt, which the memory keeps in ``agentic_memory`` (and in the link
    to A for a commit accepted at once), was returned by ``alice_memory_review`` by id, the MCP pack and the HTTP pack to
    every key that may not read B, and ``alice_explain`` did not refuse. Held (approved) and confirmed commits store no
    link, so before the change of the second head they were clean on main and leaked through this spelling only.

    Run through the three doors, four spellings, ids with one, two and three zeros, every key, every reader. A control reads
    each surface before the change; the keys that may still read B keep the quote, the others get none, ``alice_explain``
    agrees, and the owner is shown what was stored.

    Mutation: in ``_uuid_text`` (``vnext_source_fence.py``) read only the stripped text (``for candidate in (text.strip(),)``):
    every case fails; read only ``text`` in ``_whole_id`` (``for whole in (text,)``): the newline case fails; let
    ``_SOURCE_PREFIXES`` take the whitespace after the prefix again: the cases with ``source:`` and a space or tab fail.
    """

    allowed, restricted = _two_sources_with_a_zero_led_b(vault, zeros)
    tag = "zero" + "".join(ch for ch in f"{door}{shape}{zeros}{variant}".lower() if ch.isalpha())
    refs = _ZERO_LED_SHAPES[shape](allowed, restricted)
    memory_id, query = _commit_through(vault, door, refs, tag=tag)
    linked = _link_count(vault, memory_id) > 0
    assert linked is (door == "http_commit")

    before = {
        (who, surface): _holds_quote(answer)
        for who in _KEY_SPECS
        for surface, answer in _surfaces(vault, who, memory_id, query).items()
    }
    for (who, surface), held in before.items():
        assert held is (linked or not surface.startswith("pack")), ("the control", door, shape, who, surface)
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _surfaces(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (door, shape, variant, who, surface, str(answer["text"])[:300])
            assert _holds_quote(answer) is (before[(who, surface)] and who in authorized), (door, shape, variant, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (door, shape, variant, who)
    for surface, answer in _surfaces(vault, None, memory_id, query).items():
        assert _holds_quote(answer) is (linked or not surface.startswith("pack")), ("the owner", door, shape, variant, surface)
