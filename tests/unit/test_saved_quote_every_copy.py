"""Every copy of a saved quote is withheld with the quote, on a real vault, with real keys.

Unreleased (on main, not in v0.20.0). ``tests/unit/test_saved_quotes_follow_the_source_fence.py`` runs the lifecycle of a
quote saved from one source. An outside review found the case it left open: a commit with ``{"source_ids": [A, B]}`` makes
a provenance link to A only, and saves the one excerpt as the quote of that link and as the memory's own
``agentic_memory.conversation_excerpt``, which names both sources. After B was made confidential or archived the memory's
copy was withheld and the link to A, which the caller may read, kept the same bytes: ``alice_memory_review`` by id, the
MCP context pack and the HTTP context pack returned them to every key that could not read B. The same quote was also
visible through every ref the reader did not parse at all (an upper case ``SOURCE:``, ``selected_source_ids``, an id under
another key, several ids in one string), where the memory's own copy and ``alice_explain`` leaked as well.

The tests run each writer and each shape on SQLite with real minted agent keys, reclassify B with the shipped
``review_vnext_source`` handler, and call every reader as every kind of key, searching the serialized answer for the unique
quote. A control reads each surface before the change and finds the quote there, so an absent quote was withheld and
was not missing from the surface. Each test names the mutation that must fail it. The mutations were made by hand in a
scratch edit and the file was restored by copying the saved copy back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_source_fence import _quote_text
from tests.unit.test_saved_quotes_follow_the_source_fence import (
    _AGENT_IDS,
    _AUTHORIZED_AFTER,
    _KEY_SPECS,
    _QUOTE,
    _USER_ID,
    _VARIANTS,
    _WORD_A,
    _Vault,
    _holds_quote,
    _readers,
    vault,  # noqa: F401  (a fixture of the sibling module)
)

_ALLOWED_TEXT = "Alpha second log. Operator note: glaze shelf inventory nine jars."
_QUERY_WORDS = "cone ten firing schedule {tag} wall calendar"

# ``(refs, is_incidental)``: how a commit cites the allowed source ``a`` and the restricted source ``b``. A shape is
# *incidental* when ``b`` is named under a key that does not hold a source reference, where the reader judges an id only if
# it names a stored source (so an archived or deleted ``b`` is not judged there: see ``test_a_limit_...``).
_Shape = tuple[Callable[[str, str], list[object]], bool]
_SHAPES: dict[str, _Shape] = {
    "nested ids a,b": (lambda a, b: [{"source_ids": [a, b]}], False),
    "nested ids b,a": (lambda a, b: [{"source_ids": [b, a]}], False),
    "two separate refs": (lambda a, b: [a, b], False),
    "two separate refs reversed": (lambda a, b: [b, a], False),
    "list in list": (lambda a, b: [[a, b]], False),
    "selected_source_ids": (lambda a, b: [{"source_id": a, "selected_source_ids": [b]}], False),
    "source_references": (lambda a, b: [a, {"source_references": [b]}], False),
    "list under a scalar key": (lambda a, b: [{"source_id": [a, b]}], False),
    "nested under an unlisted key": (lambda a, b: [{"source_id": a, "meta": {"source_ids": [b]}}], False),
    "upper case SOURCE prefix": (lambda a, b: [a, "SOURCE:" + b], False),
    "Source prefix": (lambda a, b: [a, "Source:" + b], False),
    "doubled source prefix": (lambda a, b: [a, "source:source:" + b], False),
    "upper case URN": (lambda a, b: [a, "URN:UUID:" + b.upper()], False),
    "alice URL": (lambda a, b: [a, "alice://sources/" + b], False),
    "chunk suffix": (lambda a, b: [a, f"source:{b}#chunk-1"], False),
    "padded under a key": (lambda a, b: [{"source_id": a, "id": f"  {b}  "}], False),
    "comma joined": (lambda a, b: [f"{a},{b}"], False),
    "JSON text": (lambda a, b: [json.dumps({"source_ids": [a, b]})], False),
    "no hyphens": (lambda a, b: [a, b.replace("-", "")], False),
    "upper case id": (lambda a, b: [a, b.upper()], False),
    "braces": (lambda a, b: [a, "{" + b + "}"], False),
    "urn:uuid:": (lambda a, b: [a, "urn:uuid:" + b], False),
    "another key": (lambda a, b: [{"source_id": a, "origin": b}], True),
    "ids as dict values": (lambda a, b: [{"source_ids": {"first": a, "second": b}}], True),
    "url under sources": (lambda a, b: [{"source_id": a, "sources": [{"url": "source:" + b}]}], True),
}
# The shapes run through every change of label. The rest run through the two that matter most (confidential: a key
# below the ceiling; archived: nobody reads it), to keep the suite short.
_CORE = (
    "nested ids a,b",
    "selected_source_ids",
    "upper case SOURCE prefix",
    "comma joined",
    "another key",
    "two separate refs",
)


def _commit_with_refs(
    vault: _Vault,
    refs: list[object],
    *,
    tag: str,
    excerpt: str = _QUOTE,
    confidence: float = 0.95,
    expect: int = 201,
) -> tuple[str, str, dict[str, object]]:
    """``POST /v0/vnext/memories/commit`` with the refs as given (the MCP tool takes strings only and has no excerpt)."""

    from alicebot_api.routers import vnext_memories as router

    response = router.commit_vnext_memory(
        router.VNextMemoryCommitRequest(
            user_id=UUID(_USER_ID),
            title=f"Cone ten firing note {tag}",
            canonical_text=f"The cone ten firing schedule {tag} is posted on the wall calendar.",
            memory_type="project_fact",
            domain="project",
            sensitivity="internal",
            confidence=confidence,
            source_refs=refs,
            conversation_excerpt=excerpt,
            agent_id=_AGENT_IDS["trusted"],
        ),
        authorization=f"Bearer {vault.keys['trusted']}",
    )
    body = json.loads(response.body)
    assert response.status_code == expect, body
    return str(body["memory"]["id"]), _QUERY_WORDS.format(tag=tag), body


def _two_sources(vault: _Vault) -> tuple[str, str]:
    """The source the keys may keep reading (``a``) and the one that is reclassified later (``b``)."""

    restricted = vault.capture_source()
    allowed = vault.capture_source(_ALLOWED_TEXT, "Second log")
    return allowed, restricted


def _link_count(vault: _Vault, memory_id: str) -> int:
    return len(vault.sql("SELECT id FROM provenance_links WHERE target_id = ?", (memory_id,)))


# -- 1. the cases of the outside review --------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["review", "pack", "http_pack"])
@pytest.mark.parametrize("variant", ["confidential", "archived"])
def test_a_nested_reference_cannot_leave_the_hidden_quote_on_the_link_to_the_readable_source(
    vault: _Vault, surface: str, variant: str
) -> None:
    """The six cases of the review, as written. A commit with ``source_refs=[{"source_ids": [A, B]}]`` stores both ids in the
    memory and one link, to A, whose quote is the excerpt. B is then made confidential or archived. A key bound to the
    project that may read A but not B read the quote from ``alice_memory_review`` by id (the link), from the MCP context
    pack and from the HTTP context pack (``supporting_evidence``), although the memory's own copy was already withheld and
    ``alice_explain`` refused. The control reads the same surface before the change, and for a confidential B the admin
    key (which may read it) keeps the quote.

    Mutation: drop ``texts |= _memory_copy_quote_texts(row)`` in ``SavedProvenanceReader._verdict``: all six cases fail
    (this is the rule of the first version of the change, which compared a link only with the links that were left out).
    """

    restricted, allowed = vault.capture_source(), vault.capture_source(_ALLOWED_TEXT, "Second log")
    memory_id, query, _body = _commit_with_refs(vault, [{"source_ids": [allowed, restricted]}], tag="nestedref")
    links = vault.sql("SELECT source_id, quote FROM provenance_links WHERE target_id = ?", (memory_id,))
    assert len(links) == 1 and str(links[0]["source_id"]) == allowed, "the commit links the first id of the ref only"

    def read() -> dict[str, object]:
        return getattr(vault, surface)("project", memory_id if surface == "review" else query)

    before = read()
    assert not before["is_error"] and _WORD_A in str(before["text"]), before
    vault.reclassify(restricted, variant)
    assert vault.explain("project", memory_id)["is_error"], "explain refuses the same source relationship"
    if variant == "confidential":
        admin_control = vault.review("admin", memory_id)
        assert not admin_control["is_error"] and _WORD_A in str(admin_control["text"])
    after = read()
    assert not after["is_error"], after
    if surface == "review":
        memory = after["payload"]["review"]["memory"]  # type: ignore[index]
        assert "conversation_excerpt" not in memory["metadata_json"]["agentic_memory"]
    assert _WORD_A not in str(after["text"]), after["payload"]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_nested_reference_is_withheld_from_every_key_that_may_not_read_b_and_kept_for_the_rest(
    vault: _Vault, variant: str
) -> None:
    """The case above for every key and every change of label (confidential, private, health, moved to project ``beta``,
    archived), at every reader the pack has (the MCP pack and its deep tier, the HTTP pack and its deep tier) and the
    review. A key that may read B keeps the quote on every surface where it was before, a key that may not gets no byte of
    it, ``alice_explain`` agrees with the table, the link to A is still returned (its id and role), and the memory stays in
    every pack. The owner is shown what was stored. After the change the admin key's review of a confidential B is the
    owner's review, byte for byte.

    Mutations: as the test above, and make the test in ``SavedProvenanceReader._shown`` read ``if False:``: the link keeps
    its quote for every key.
    """

    allowed, restricted = _two_sources(vault)
    memory_id, query, _body = _commit_with_refs(vault, [{"source_ids": [allowed, restricted]}], tag="matrixnested")
    before = {
        (who, surface): _holds_quote(answer)
        for who in _KEY_SPECS
        for surface, answer in _readers(vault, who, memory_id, query).items()
    }
    assert all(before.values()), before
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (who, surface)
            assert _holds_quote(answer) is (who in authorized), (variant, who, surface)
            if surface != "review":
                rows = answer["payload"]["relevant_memories" if surface.startswith("http") else "memories"]  # type: ignore[index]
                assert memory_id in [str(row["id"]) for row in rows], (variant, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (variant, who)
        links = vault.review(who, memory_id)["payload"]["review"]["provenance_links"]  # type: ignore[index]
        assert [str(link["source_id"]) for link in links] == [allowed], (variant, who)
        assert (links[0]["quote"] is not None) is (who in authorized), (variant, who)
    for surface, answer in _readers(vault, None, memory_id, query).items():
        assert _holds_quote(answer), ("the owner is shown what was stored", variant, surface)
    if variant == "confidential":
        assert vault.review("admin", memory_id)["payload"] == vault.review(None, memory_id)["payload"]


# -- 2. every shape a writer stores -------------------------------------------------------------------------------


def _shape_params() -> list[object]:
    params: list[object] = []
    for label, (_build, incidental) in _SHAPES.items():
        for variant in _VARIANTS:
            if label not in _CORE and variant not in ("confidential", "archived"):
                continue
            if incidental and variant == "archived":
                continue  # the stated limit, pinned by its own test
            params.append(pytest.param(label, variant, id=f"{label}-{variant}"))
    return params


@pytest.mark.parametrize(("label", "variant"), _shape_params())
def test_a_ref_in_any_shape_that_names_b_withholds_the_quote_from_a_key_that_may_not_read_b(
    vault: _Vault, label: str, variant: str
) -> None:
    """Each shape a client can send in ``source_refs`` (the commit route stores the list raw): a multi-id ref in
    ``source_ids``, ``source_references`` or ``selected_source_ids``, a list or an object under a key that holds one id,
    refs nested under a key the reader does not know, an upper case ``SOURCE:`` or ``Source:`` prefix, a doubled prefix,
    ``URN:UUID:``, a URL, a chunk suffix, a padded id, several ids in one string or in a JSON string, and the spellings the
    link writer read before (no hyphens, upper case, braces, ``urn:uuid:``), with ``b`` in each. The link writer reads
    some of them (and links ``a``, or both ids) and none of the others (and the memory has no link), so the quote can be on
    a link, in the memory's own copy, or both. Whatever it is in, a key that may not read ``b`` is shown none of it by the
    review, the MCP pack, the HTTP pack and the deep tier of the HTTP pack, ``alice_explain`` refuses it, and a key that may
    read ``b`` keeps what it had. The MCP pack carries only the link, so for a memory with no link it carries nothing to
    withhold and nothing to leak. The owner is shown what was stored.

    Mutations, each alone, in ``vnext_source_fence.py``: drop ``selected_source_ids`` from ``SOURCE_REFERENCE_KEYS`` (the
    ``selected_source_ids`` rows fail); drop the ``.lower()`` of ``UUID(candidate.lower())`` in ``_whole_id`` (the upper case
    URN rows fail); make the ``source:`` prefix case sensitive in ``_whole_id`` and in ``_SOURCE_MARKER`` together (the
    upper case ``SOURCE:`` and ``Source:`` rows fail when ``b`` is archived); iterate only the first entry of a list in
    ``cited_source_ids`` (every row with a second id fails); make ``_json_container`` return None (the JSON row fails when
    ``b`` is archived); drop the ``cited.incidental`` set from ``refused`` in ``_verdict`` (the ``another key`` rows fail).
    """

    build, _incidental = _SHAPES[label]
    allowed, restricted = _two_sources(vault)
    tag = "shape" + "".join(ch for ch in label.lower() if ch.isalpha())
    memory_id, query, _body = _commit_with_refs(vault, build(allowed, restricted), tag=tag)
    linked = _link_count(vault, memory_id) > 0
    deep = label in _CORE

    def surfaces(who: str | None) -> dict[str, dict[str, object]]:
        out = {"review": vault.review(who, memory_id), "pack": vault.pack(who, query)}
        if deep:
            out["pack_deep"] = vault.pack_deep(who, query)
        if who is not None:
            out["http_pack"] = vault.http_pack(who, query)
            if deep:
                out["http_pack_deep"] = vault.http_pack(
                    who, query, context_depth="high", include_sources=False, include_contradictions=True
                )
        return out

    before = {(who, surface): _holds_quote(answer) for who in _KEY_SPECS for surface, answer in surfaces(who).items()}
    for (who, surface), held in before.items():
        assert held is (linked or not surface.startswith("pack")), ("the control", label, who, surface)
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in surfaces(who).items():
            assert answer["is_error"] is False, (label, variant, who, surface, str(answer["text"])[:300])
            assert _holds_quote(answer) is (before[(who, surface)] and who in authorized), (label, variant, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (label, variant, who)
    for surface, answer in surfaces(None).items():
        assert _holds_quote(answer) is (linked or not surface.startswith("pack")), ("the owner", label, variant, surface)


def test_a_limit_an_id_under_an_unlisted_key_that_names_an_archived_source_is_not_judged(vault: _Vault) -> None:
    """The stated limit of the reader. An id under a key that does not hold a source reference (``origin``) is judged when it
    names a stored source and left alone when it names none, because it may be a chunk id or a session id. A source that
    was archived (or deleted) is not returned by either store, so such an id cannot be told from one that names no source,
    and the quote stays for a key that may read the other source. The same ref with the id under ``source_ids`` is
    judged (the shape table above). Closing this needs a store read that returns archived sources; if one is added, this
    test fails and is turned into the case above for the ``another key`` shape.

    Mutation: none to make; this pins a limit, and it fails when the limit is closed.
    """

    allowed, restricted = _two_sources(vault)
    memory_id, _query, _body = _commit_with_refs(vault, [{"source_id": allowed, "origin": restricted}], tag="limitorigin")
    vault.reclassify(restricted, "archived")
    answer = vault.review("trusted", memory_id)
    assert answer["is_error"] is False
    assert _holds_quote(answer), "the stated limit: an archived source named under an unlisted key is not judged"
    assert vault.explain("trusted", memory_id)["is_error"] is False


# -- 3. long and odd excerpts --------------------------------------------------------------------------------------


def _words(length: int, *, start: str = "tkn-start", middle: str = "tkn-middle", end: str = "tkn-end") -> str:
    """An excerpt of exactly ``length`` characters with a distinct token at each end and in the middle."""

    filler = "lorem ipsum dolor sit amet "
    half = (length - len(middle) - 2) // 2
    head = (start + " " + filler * (half // len(filler) + 1))[:half]
    tail = (filler * (half // len(filler) + 2))[: length - half - len(middle) - 2 - len(end) - 1]
    text = f"{head} {middle} {tail} {end}"
    assert len(text) == length, (len(text), length)
    return text


_LONG_EXCERPTS = {
    "odd whitespace": "  tkn-start lead\tTAB nb sp em sp\r\nCRLF\n\nblank tkn-middle tkn-end  ",
    "3900 characters": _words(3900),
    "4000 characters": _words(4000),
}


@pytest.mark.parametrize("kind", list(_LONG_EXCERPTS))
def test_a_long_or_odd_excerpt_is_withheld_whole_from_the_link_and_the_copy(vault: _Vault, kind: str) -> None:
    """The excerpt reaches the link and the copy as one string, whatever its length or whitespace: a tab, a no-break space,
    an em space and CRLF are collapsed once by the commit door and the 4,000 characters the HTTP model allows arrive whole.
    The two strings are equal, so the rule withholds the link's quote when the copy is withheld. A token at the start, in the
    middle and at the end of the excerpt is searched for, so a cut copy shows too. The admin key, which may read B,
    keeps the three tokens in the link and in the copy.

    Mutation: cut the quote of the link to 3,000 characters in ``_create_provenance_links`` (``vnext_memory_commit.py``): the
    two long excerpts fail here (the link is no longer the copy, so it keeps its quote) and in the producer test below.
    """

    allowed, restricted = _two_sources(vault)
    excerpt = _LONG_EXCERPTS[kind]
    memory_id, query, _body = _commit_with_refs(
        vault, [{"source_ids": [allowed, restricted]}], tag="longexcerpt", excerpt=excerpt
    )
    (link,) = vault.sql("SELECT quote FROM provenance_links WHERE target_id = ?", (memory_id,))
    copy = json.loads(vault.sql("SELECT metadata_json FROM memories WHERE id = ?", (memory_id,))[0]["metadata_json"])[
        "agentic_memory"
    ]["conversation_excerpt"]
    assert link["quote"] == copy and _quote_text(link["quote"]) == _quote_text(excerpt)
    tokens = ("tkn-start", "tkn-middle", "tkn-end")

    def tokens_in(answer: dict[str, object]) -> set[str]:
        payload = answer["payload"]
        if isinstance(payload, dict) and "sources" in payload:
            payload = {key: value for key, value in payload.items() if key != "sources"}
        text = json.dumps(payload)
        return {token for token in tokens if token in text}

    surfaces = {
        "review": lambda who: vault.review(who, memory_id),
        "http_pack": lambda who: vault.http_pack(who, query),
    }
    for surface, read in surfaces.items():
        assert tokens_in(read("trusted")) == set(tokens), ("the control", surface)
    vault.reclassify(restricted, "confidential")
    for surface, read in surfaces.items():
        assert tokens_in(read("trusted")) == set(), surface
        assert tokens_in(read("project")) == set(), surface
        assert tokens_in(read("admin")) == set(tokens), surface


# -- 4. every door ------------------------------------------------------------------------------------------------


def _correct(vault: _Vault, **arguments: object) -> dict[str, object]:
    done = vault.wire("alice_memory_correct", dict(arguments), who=vault.reviewer)
    assert done["is_error"] is False, done
    return done


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_a_reviewer_who_copies_the_excerpt_of_a_held_commit_onto_a_readable_source_cannot_leak_it(
    vault: _Vault, variant: str
) -> None:
    """A commit held for review cited B and kept the excerpt as its own copy. The reviewer edits and approves it with a
    ``provenance`` that names the readable source A and quotes the same excerpt, so the link to A carries B's bytes. When B
    is made confidential or archived the copy is withheld (it names B) and the link to A used to keep the text. Now the link
    loses it too.

    Mutation: as the first test of this file (drop the copy texts in ``_verdict``).
    """

    allowed, restricted = _two_sources(vault)
    memory_id, query, body = _commit_with_refs(vault, [restricted], tag="heldreview", confidence=0.4, expect=201)
    assert body["status"] == "review_required", body
    _correct(
        vault,
        review_item_id=memory_id,
        action="edit-and-approve",
        provenance={"source_id": allowed, "quote": _QUOTE},
    )
    links = vault.sql("SELECT source_id, quote FROM provenance_links WHERE target_id = ?", (memory_id,))
    assert [str(link["source_id"]) for link in links] == [allowed] and links[0]["quote"] == _QUOTE
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        assert _holds_quote(vault.review(who, memory_id)), ("the control", who)
        assert _holds_quote(vault.pack(who, query)), ("the control", who)
    vault.reclassify(restricted, variant)
    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert _holds_quote(answer) is (who in authorized), (variant, who, surface)


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_the_superseded_memory_of_a_nested_reference_withholds_its_quote_like_the_replacement(
    vault: _Vault, variant: str
) -> None:
    """Superseding a memory that cited ``[A, B]`` in one ref leaves the old memory (status superseded) with its link to A
    and the excerpt. After B is made confidential or archived the review of the old memory withholds the quote from a key
    that may not read B, as the review of its replacement does.

    Mutation: as the first test of this file.
    """

    allowed, restricted = _two_sources(vault)
    old_id, _query, _body = _commit_with_refs(vault, [{"source_ids": [allowed, restricted]}], tag="supersedeold")
    done = _correct(
        vault,
        review_item_id=old_id,
        action="supersede-existing",
        replacement_title="Cone ten firing v2",
        replacement_body={"text": "The cone ten firing schedule is posted by the kiln door."},
        reason="moved",
    )
    new_id = str(done["payload"]["replacement_object"]["id"])  # type: ignore[index]
    assert vault.sql("SELECT status FROM memories WHERE id = ?", (old_id,))[0]["status"] == "superseded"
    assert _holds_quote(vault.review("trusted", old_id)), "the control"
    held_by_the_replacement = _holds_quote(vault.review("trusted", new_id))
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        assert _holds_quote(vault.review(who, old_id)) is (who in authorized), (variant, who)
        assert _holds_quote(vault.review(who, new_id)) is (held_by_the_replacement and who in authorized), (variant, who)


@pytest.mark.parametrize("door", ("held", "confirmed"))
@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_a_held_or_confirmed_commit_with_a_nested_reference_has_no_link_and_withholds_its_copy(
    vault: _Vault, door: str, variant: str
) -> None:
    """The two doors that make no link, with a nested ref: held for review (confidence under 0.5) and approved, or
    confirmed by the author (under 0.85). The memory keeps the ids and the excerpt only in its own copies, so the reader
    judges it by every id inside the nested ref. A key that may not read B gets no byte of the excerpt from the review or
    the HTTP pack and ``alice_explain`` refuses it; the key that may read B keeps them.

    Mutation: make ``cited_source_ids`` skip the second entry of a list (``node[:1]``): the nested ref names only A and the
    key that may not read B keeps the copy.
    """

    allowed, restricted = _two_sources(vault)
    refs: list[object] = [{"source_ids": [allowed, restricted]}]
    if door == "held":
        memory_id, query, body = _commit_with_refs(vault, refs, tag="heldnested", confidence=0.4)
        assert body["status"] == "review_required", body
        _correct(vault, review_item_id=memory_id, action="approve", reason="check")
    else:
        memory_id, query, body = _commit_with_refs(vault, refs, tag="confirmednested", confidence=0.7)
        assert body["status"] == "confirmation_required", body
        confirmed = vault.wire(
            "alice_memory_commit",
            {"confirmation_id": body["confirmation"]["confirmation_id"], "confirmation_action": "confirm"},  # type: ignore[index]
            who="trusted",
        )
        assert confirmed["is_error"] is False, confirmed
    assert _link_count(vault, memory_id) == 0, "neither door makes a link"
    for who in _KEY_SPECS:
        assert _holds_quote(vault.review(who, memory_id)) and _holds_quote(vault.http_pack(who, query)), ("control", who)
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        assert _holds_quote(vault.review(who, memory_id)) is (who in authorized), (door, variant, who)
        assert _holds_quote(vault.http_pack(who, query)) is (who in authorized), (door, variant, who)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (door, variant, who)


# -- 5. nothing changes for a caller who may read every source ----------------------------------------------------


def test_a_caller_who_may_read_every_source_is_shown_exactly_what_the_owner_is_shown_for_refs_that_hold_other_ids(
    vault: _Vault,
) -> None:
    """The refs below name one readable source and hold ids that are not sources: a chunk id under ``chunk_id``, a page and a
    URL, a ``memory:`` ref (the rollups write these), a ``session:`` ref and an id inside the quote's own text. None of them
    is a stored source, so none is refused, and every key bound to the project gets the review of the owner, byte for
    byte, the pack with its quote, and an ``alice_explain`` answer. The same memory read after an unrelated source is made
    confidential is unchanged.

    Mutation: judge an incidental id like a named one (in ``SavedProvenanceReader._verdict``, refuse ``cited.incidental``
    ids that are not stored): every key loses the quote and explain refuses.
    """

    allowed, restricted = _two_sources(vault)
    refs: list[object] = [
        {"source_id": allowed, "chunk_id": str(uuid4()), "page": 3, "url": "https://example.test/doc"},
        f"memory:{uuid4()}",
        f"session:{uuid4()}",
        "meeting notes",
    ]
    memory_id, query, _body = _commit_with_refs(vault, refs, tag="otherids")
    owner_review = vault.review(None, memory_id)["payload"]
    assert _WORD_A in json.dumps(owner_review)
    for who in _KEY_SPECS:
        assert vault.review(who, memory_id)["payload"] == owner_review, who
        assert _holds_quote(vault.pack(who, query)) and _holds_quote(vault.http_pack(who, query)), who
        assert vault.explain(who, memory_id)["is_error"] is False, who
    vault.reclassify(restricted, "confidential")
    for who in _KEY_SPECS:
        assert vault.review(who, memory_id)["payload"] == owner_review, ("an unrelated source changed", who)


def test_the_text_of_a_quote_is_not_read_as_a_reference(vault: _Vault) -> None:
    """A reviewer's quote is text from a source and can hold anything, an id of another source included. After an
    edit-and-approve whose quote mentions the id of a source ``c`` that is later made confidential, a key that may read the
    cited source still gets its quote (the quote says what it says; it does not cite ``c``) and ``alice_explain`` answers.

    Mutation: remove the ``_TEXT_KEYS`` skip in ``cited_source_ids``: the quote names ``c``, ``c`` is refused, and the
    key loses the quote and the explain answer.
    """

    cited = vault.capture_source()
    mentioned = vault.capture_source(_ALLOWED_TEXT, "Second log")
    candidate = vault._candidate("Quotetextnote: the alpha kiln is fired on Mondays")
    _correct(
        vault,
        review_item_id=candidate,
        action="edit-and-approve",
        body={"text": "Kiln schedule: the alpha kiln is fired on Mondays."},
        provenance={"source_id": cited, "quote": f"{_QUOTE} see also {mentioned}"},
    )
    vault.reclassify(mentioned, "confidential")
    for who in ("trusted", "project"):
        assert _holds_quote(vault.review(who, candidate)), who
        assert vault.explain(who, candidate)["is_error"] is False, who


def test_the_owner_is_shown_what_was_stored_for_a_nested_reference(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner, a call with no agent key, is not fenced: after B is made confidential and after it is archived the owner
    still reads the quote from the review, the MCP pack and the HTTP pack (a vault with no key at all, since the HTTP route
    refuses a keyless call once any key exists), and the link to A keeps its quote.

    Mutation: make ``SourceReadFence.fenced`` return ``True``: the owner's link loses its quote.
    """

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID))
    assert _sqlite_path_from_url(context.database_url)
    owner_vault = _Vault(context, monkeypatch, with_keys=False)
    allowed, restricted = _two_sources(owner_vault)
    from alicebot_api.routers import vnext_memories as router

    response = router.commit_vnext_memory(
        router.VNextMemoryCommitRequest(
            user_id=UUID(_USER_ID),
            title="Cone ten firing owner",
            canonical_text="The cone ten firing schedule ownernested is posted on the wall calendar.",
            memory_type="project_fact",
            domain="project",
            sensitivity="internal",
            confidence=0.95,
            source_refs=[{"source_ids": [allowed, restricted]}],
            conversation_excerpt=_QUOTE,
        ),
        authorization=None,
    )
    assert response.status_code == 201, response.body
    memory_id = str(json.loads(response.body)["memory"]["id"])
    query = _QUERY_WORDS.format(tag="ownernested")
    for variant in ("confidential", "archived"):
        owner_vault.reclassify(restricted, variant)
        for surface, answer in {
            "review": owner_vault.review(None, memory_id),
            "pack": owner_vault.pack(None, query),
            "http_pack": owner_vault.http_pack(None, query),
        }.items():
            assert answer["is_error"] is False, (variant, surface, str(answer["text"])[:300])
            assert _holds_quote(answer), (variant, surface)
        links = owner_vault.review(None, memory_id)["payload"]["review"]["provenance_links"]  # type: ignore[index]
        # The review frames each stored string as data, so the quote is inside the framing.
        assert [_QUOTE in str(link["quote"]) for link in links] == [True], (variant, links)


# -- 6. what the writers store, derived by running them ---------------------------------------------------------


def _stored_pair(vault: _Vault, memory_id: str, copy_path: tuple[str, ...], *, role: str | None = None) -> tuple[str, str]:
    """The quote of the memory's link (of ``role``, when it has several) and the copy at ``copy_path`` of its metadata."""

    query = "SELECT quote FROM provenance_links WHERE target_id = ?"
    args: tuple[object, ...] = (memory_id,)
    if role is not None:
        query += " AND evidence_role = ?"
        args = (memory_id, role)
    (link,) = vault.sql(query, args)
    node: object = json.loads(vault.sql("SELECT metadata_json FROM memories WHERE id = ?", (memory_id,))[0]["metadata_json"])
    for key in copy_path:
        node = node[key]  # type: ignore[index]
    return str(link["quote"]), str(node)


_PRODUCER_EXCERPTS = {
    "odd whitespace": "  lead\tTAB nb sp em sp\r\nCRLF\n\nblank tkn-odd  ",
    "3900 characters": _words(3900),
    "4000 characters": _words(4000),
}


@pytest.mark.parametrize("kind", list(_PRODUCER_EXCERPTS))
def test_every_writer_saves_the_same_string_on_the_link_and_in_the_copy_and_cuts_nothing(vault: _Vault, kind: str) -> None:
    """The derivation of the match rule, kept as a test. Each writer that saves a quote is run on SQLite with an excerpt and
    the string on the link is compared with the string in the memory's copy.

    * The commit route (``POST /v0/vnext/memories/commit``; the command line and the legacy MCP tool reach the same
      ``memory_commit_request_from_payload``) collapses the whitespace once and stores that string as the quote of the
      link and as ``agentic_memory.conversation_excerpt``: the two are equal, and nothing is cut (3,900 characters stay
      3,900, an excerpt of 4,000 characters, the longest the model takes, stays whole once collapsed).
    * An edit-and-approve review and a supersede review store the quote as sent, as the link's quote and as
      ``provenance.quote`` / ``replacement_provenance.quote``: equal, whitespace and all.
    * A commit held for review or confirmed inline stores the collapsed excerpt as its copy and no link.

    So the match between a link's quote and a copy is equality of the words, and a link whose quote is a part of an
    excerpt is not the same quote.

    Mutations: cut the quote of the link in ``_create_provenance_links`` (``vnext_memory_commit.py``) to 3,000 characters
    (the commit pair differs), or cut ``provenance.quote`` in ``_validated_review_provenance`` (``mcp/review.py``) to 3,000
    characters (the review pair differs): the 3,900 and 4,000 character cases fail.
    """

    excerpt = _PRODUCER_EXCERPTS[kind]
    allowed, _restricted = _two_sources(vault)

    memory_id, _query, _body = _commit_with_refs(vault, [allowed], tag="producercommit", excerpt=excerpt)
    link_quote, copy = _stored_pair(vault, memory_id, ("agentic_memory", "conversation_excerpt"))
    assert link_quote == copy == " ".join(excerpt.split()), "the commit door stores one collapsed string twice"

    candidate = vault._candidate(f"Producerreview{kind.split()[0]}: the alpha kiln is fired on Mondays")
    _correct(
        vault,
        review_item_id=candidate,
        action="edit-and-approve",
        body={"text": "Kiln schedule: the alpha kiln is fired on Mondays."},
        provenance={"source_id": allowed, "quote": excerpt},
    )
    link_quote, copy = _stored_pair(vault, candidate, ("provenance", "quote"), role="supports")
    assert link_quote == copy == excerpt, "a review stores the quote as sent, on the link and in the copy"

    candidate = vault._candidate(f"Producersupersede{kind.split()[0]}: the alpha glaze shelf was reorganised on Friday")
    done = _correct(
        vault,
        review_item_id=candidate,
        action="supersede-existing",
        replacement_title="Alpha glaze shelf v2",
        replacement_body={"text": "The alpha glaze shelf was reorganised on Saturday morning."},
        replacement_provenance={"source_id": allowed, "quote": excerpt},
        reason="moved",
    )
    replacement = str(done["payload"]["replacement_object"]["id"])  # type: ignore[index]
    link_quote, copy = _stored_pair(vault, replacement, ("replacement_provenance", "quote"))
    assert link_quote == copy == excerpt

    held_id, _query, body = _commit_with_refs(vault, [allowed], tag="producerheld", excerpt=excerpt, confidence=0.4)
    assert body["status"] == "review_required" and _link_count(vault, held_id) == 0
    copy = json.loads(vault.sql("SELECT metadata_json FROM memories WHERE id = ?", (held_id,))[0]["metadata_json"])[
        "agentic_memory"
    ]["conversation_excerpt"]
    assert copy == " ".join(excerpt.split())
