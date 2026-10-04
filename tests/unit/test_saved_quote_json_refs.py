"""A ref that is JSON text is read as the value it decodes to, so the quote inside it is not a citation.

Unreleased (on main, not in v0.20.0). An outside review of the saved-quote reader found that ``cited_source_ids`` scanned
the raw text of a ref that is a JSON string before it decoded it. An id in the ``quote`` field of such a ref was then read as
a citation: ``'{"source_id": "<A>", "quote": "Log line: source:<an id that names no source> is a literal marker"}'`` named a
source that does not exist, and a named id that names no stored source counts as missing, so the quote the memory saved
from ``A`` disappeared from ``alice_memory_review`` by id, from the MCP context pack and from the HTTP context pack, and
``alice_explain`` refused the memory. The same ref stored as an object was read correctly, because the walker of an object
skips a ``quote``. The over-withholding went one way only: nothing was shown that should not have been.

The rule now: a ref string that, with its whitespace stripped, starts with ``{`` or ``[`` and decodes (``json.loads``) to an
object or a list is read as that object or list, by the walker that reads the same value stored as one, and its raw text is
not scanned. A string that does not decode (a truncated text, a trailing comma, an id in braces) keeps the scan of its text.
The link writer (``source_uuids_in_ref``) never decodes JSON, and no JSON text is an id it reads (see the fuzz below), so
the reader stays a superset of the writer.

The tests are in four groups. A real SQLite vault with real agent keys runs the case of the review through every encoding
of the ref and every reader; the same vault checks that an id in a field other than a quote is judged exactly as it is in
an object, and that a text that does not decode keeps the scan. Unit tests on ``cited_source_ids`` hold the JSON text equal
to the decoded value on random refs (an independent judge: the read of the object), the quote and excerpt keys, repeated
keys, the depth and the cost. A differential fuzz holds the reader a superset of the writer on refs that contain JSON text.
A stub store checks the reader and the audit that ``alice_explain`` judges.

Each test names the mutation that must fail it. The mutations were made by hand on a copy of the module and the file was
restored by copying the saved original back.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from uuid import uuid4

import pytest

from alicebot_api.vnext_source_fence import (
    _json_container,
    cited_source_ids,
    cited_source_ids_in_memory_audit,
    source_uuids_in_ref,
)
from tests.unit.test_saved_provenance_reader import _reader, _revision
from tests.unit.test_saved_quote_copies_reader import _MemoryStore, _row
from tests.unit.test_saved_quote_every_copy import _commit_with_refs, _link_count, _two_sources
from tests.unit.test_saved_quote_ref_reading import (
    _LARGE_IDS,
    _SMALL_IDS,
    _new_ids,
    _random_ref,
    _zero_led_id,
    _zero_led_spellings,
    assert_cost_grows_linearly,
)
from tests.unit.test_saved_quotes_follow_the_source_fence import (
    _AUTHORIZED_AFTER,
    _KEY_SPECS,
    _Vault,
    _holds_quote,
    vault,  # noqa: F401  (a fixture of the sibling module)
)

_TEXT_KEYS = ("quote", "Quote", "conversation_excerpt", "CONVERSATION_EXCERPT")
_REFERENCE_KEYS = frozenset(
    {"source_id", "source_ids", "source_ref", "source_refs", "source_references", "selected_source_ids", "sources"}
)
_OWN_ID_KEYS = frozenset({"id", "ref"})


def _tag(*parts: str) -> str:
    """A word for the title and text of a memory, from the names of a case: letters and digits only, so a search finds it."""

    return "json" + re.sub(r"[^a-z0-9]", "", "".join(parts).lower())


def _quote_with_marker(stray: str) -> str:
    return f"Log line: source:{stray} is a literal marker"


def _ref_with_quote(cited: str, stray: str) -> dict[str, object]:
    """A ref in the shape of the review: the id of the source it cites and a quote that holds a literal marker."""

    return {"source_id": cited, "quote": _quote_with_marker(stray)}


# Every way the same ref can be stored. The first is the control the review found correct, the others are JSON text.
_ENCODINGS: dict[str, Callable[[dict[str, object]], object]] = {
    "object": lambda ref: ref,
    "json": json.dumps,
    "json, indented": lambda ref: json.dumps(ref, indent=2),
    "json, padded": lambda ref: "\n  " + json.dumps(ref) + " \t\n",
    "json array": lambda ref: json.dumps([ref]),
    "json under a reference key": lambda ref: json.dumps({"source_refs": [ref]}),
    "json in json": lambda ref: json.dumps({"source_refs": [json.dumps(ref)]}),
    "json under a reference key of an object": lambda ref: {"sources": json.dumps(ref)},
}


# -- 1. the case of the review, on a real vault ------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["object", "json"])
@pytest.mark.parametrize("surface", ["review", "pack", "http_pack", "explain"])
def test_literal_marker_inside_quote_is_not_a_citation(vault: _Vault, encoding: str, surface: str) -> None:
    """The probe of the review, run as it was: a commit cites a source and carries a ref whose ``quote`` holds
    ``source:<an id that names no source>``, as an object and as JSON text, and each reader is asked by a project key. The
    four ``json`` cases failed on the code of the review (the quote went from the review, from both packs, and explain
    refused) and the four ``object`` cases passed.

    Mutations, each alone, in ``cited_source_ids``: do not decode a JSON string (``nested = None``); scan the raw text of a
    string that decodes as well as reading what it decodes to (drop the ``continue`` after the decode, which is the code of
    the review). Each fails the four ``json`` cases and none of the ``object`` ones.
    """

    cited = vault.capture_source()
    nonexistent = str(uuid4())
    ref = _ref_with_quote(cited, nonexistent)
    stored_ref = json.dumps(ref) if encoding == "json" else ref
    memory_id, query = vault.http_commit_citing([cited, stored_ref], tag=f"{encoding}-{surface}")  # type: ignore[list-item]
    answer = getattr(vault, surface)("project", memory_id if surface in ("review", "explain") else query)
    assert not answer["is_error"], answer
    if surface != "explain":
        assert _holds_quote(answer), answer["payload"]


@pytest.mark.parametrize("encoding", list(_ENCODINGS))
def test_every_encoding_of_the_ref_keeps_the_quote_on_every_reader(vault: _Vault, encoding: str) -> None:
    """The ref of the review in eight encodings (an object, JSON text with and without indentation and padding, an array, a
    reference key holding the ref, JSON inside JSON, an object whose ``sources`` is JSON text): every key keeps the quote on
    the review, both packs (each at two depths) and ``alice_explain`` answers, and the key's review is the owner's review
    byte for byte. The link to the cited source is made (the writer reads the plain ref beside it) and nothing is dropped.

    Mutations, each alone: do not decode a JSON string, or scan its raw text as well (the seven JSON encodings fail); decode
    only an object, with ``("{",)`` for ``("{", "[")`` in ``_json_container`` (the encoding that is an array fails).
    """

    cited = vault.capture_source()
    stored = _ENCODINGS[encoding](_ref_with_quote(cited, str(uuid4())))
    memory_id, query, _body = _commit_with_refs(vault, [cited, stored], tag=_tag("enc", encoding))  # type: ignore[list-item]
    assert _link_count(vault, memory_id) == 1
    owner_review = vault.review(None, memory_id)["payload"]
    for who in _KEY_SPECS:
        review = vault.review(who, memory_id)
        pack = vault.pack(who, query)
        pack_deep = vault.pack_deep(who, query)
        http_pack = vault.http_pack(who, query)
        http_pack_deep = vault.http_pack(who, query, context_depth="high", include_sources=False, include_contradictions=True)
        for surface, answer in (
            ("review", review),
            ("pack", pack),
            ("pack_deep", pack_deep),
            ("http_pack", http_pack),
            ("http_pack_deep", http_pack_deep),
        ):
            assert answer["is_error"] is False, (encoding, who, surface)
            assert _holds_quote(answer), (encoding, who, surface)
        assert vault.explain(who, memory_id)["is_error"] is False, (encoding, who)
        assert review["payload"] == owner_review, (encoding, who)


# What a ref names in a field that is not a quote. Each takes the source the keys may keep (``allowed``), the one that is
# reclassified or archived later (``restricted``) and an id that names no source (``missing``), and returns the ref.
_RESTRICTED_SHAPES: dict[str, Callable[[str, str], dict[str, object]]] = {
    "a source_ids list": lambda allowed, restricted: {
        "source_ids": [allowed, restricted],
        "quote": _quote_with_marker(str(uuid4())),
    },
    "a source_id": lambda allowed, restricted: {"source_id": restricted, "quote": _quote_with_marker(str(uuid4()))},
    "an id under a free key": lambda allowed, restricted: {
        "source_id": allowed,
        "origin": restricted,
        "quote": _quote_with_marker(str(uuid4())),
    },
}


@pytest.mark.parametrize("variant", ("confidential", "archived"))
@pytest.mark.parametrize("encoding", ["object", "json"])
@pytest.mark.parametrize("shape", list(_RESTRICTED_SHAPES))
def test_an_id_outside_the_quote_that_names_a_source_the_caller_may_not_read_still_withholds_the_quote(
    vault: _Vault, shape: str, encoding: str, variant: str
) -> None:
    """Only the quote is skipped. A ref whose ``source_ids`` or ``source_id`` names a source that is then made
    confidential, or archived, or that holds its id under a free key (``origin``), is judged by that id, as an object and as
    JSON text: the keys that may not read the source lose the quote on the review and both packs and ``alice_explain``
    refuses them, and a key that may read it (the admin key, for a confidential source) keeps all of it. The quote of the ref
    holds a literal marker that names no source, so a reader that read the quote would withhold from the admin key as well.

    Mutations, each alone: do not decode a JSON string, or scan its raw text as well (the six ``json`` cases fail: the admin
    key loses the quote to the marker); read a quote of a decoded text as a reference, with the text keys renamed to
    ``sources`` in ``_keep_every_value`` (the same six fail); read nothing under a free key of a decoded text, with every key
    that is not a reference renamed to ``quote`` (the two ``origin`` cases fail: the id is not read and every key keeps the
    quote).
    """

    allowed, restricted = _two_sources(vault)
    ref = _RESTRICTED_SHAPES[shape](allowed, restricted)
    stored = ref if encoding == "object" else json.dumps(ref)
    memory_id, query, _body = _commit_with_refs(
        vault, [allowed, stored], tag=_tag("restricted", shape, encoding, variant)  # type: ignore[list-item]
    )
    surfaces = ("review", "pack", "http_pack")
    for who in _KEY_SPECS:
        for surface in surfaces:
            answer = getattr(vault, surface)(who, memory_id if surface == "review" else query)
            assert _holds_quote(answer), ("control", who, surface)
    vault.reclassify(restricted, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface in surfaces:
            answer = getattr(vault, surface)(who, memory_id if surface == "review" else query)
            assert _holds_quote(answer) is (who in authorized), (shape, encoding, variant, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in authorized), (shape, encoding, variant, who)


# An id that names no stored source, in a position that says it is a source, makes the quote go and explain refuse, whoever
# asks. The owner accepted that on 2026-10-04 for a ref stored as an object, and a JSON text is read the same way. An id that
# is only incidental (under a free key, or in a sentence of a free text field) changes nothing.
#
# The write fence refuses a ref the link writer reads when it names no stored source, so a ref stored as an object can hold
# a missing id only under a key the writer does not read (``selected_source_ids``, ``source_references``). A JSON text is a
# string to the writer, which reads nothing of it, so it can hold the missing id under any key.
_MISSING_SHAPES: dict[str, tuple[Callable[[str, str], dict[str, object]], bool, bool]] = {
    # name: (the ref from the cited source and the missing id, whether the quote is withheld, whether an object can store it)
    "a source_ids list": (
        lambda cited, missing: {"source_ids": [cited, missing], "quote": _quote_with_marker(str(uuid4()))},
        True,
        False,
    ),
    "a source_id": (
        lambda cited, missing: {"source_id": missing, "quote": _quote_with_marker(str(uuid4()))},
        True,
        False,
    ),
    "an id in a source_refs list": (
        lambda cited, missing: {"source_refs": [{"id": missing}], "source_id": cited},
        True,
        False,
    ),
    "a selected_source_ids list": (
        lambda cited, missing: {"selected_source_ids": [cited, missing], "quote": _quote_with_marker(str(uuid4()))},
        True,
        True,
    ),
    "an id in a source_references list": (
        lambda cited, missing: {"source_references": [{"id": missing}], "source_id": cited},
        True,
        True,
    ),
    "an id under a free key": (
        lambda cited, missing: {"source_id": cited, "origin": missing, "quote": _quote_with_marker(str(uuid4()))},
        False,
        True,
    ),
    "a marker in a free text field": (
        lambda cited, missing: {"source_id": cited, "note": f"copied from source:{missing}"},
        False,
        True,
    ),
}
_MISSING_CASES = [
    (shape, encoding)
    for shape, (_build, _withheld, storable_as_object) in _MISSING_SHAPES.items()
    for encoding in ("object", "json", "json array")
    if encoding != "object" or storable_as_object
]


@pytest.mark.parametrize(("shape", "encoding"), _MISSING_CASES)
def test_an_id_outside_the_quote_that_names_no_source_is_judged_as_it_is_in_an_object(
    vault: _Vault, shape: str, encoding: str
) -> None:
    """The rule the owner accepted is unchanged and does not depend on the encoding: a named id that matches no stored source
    (``source_ids``, ``source_id``, ``selected_source_ids``, the ``id`` of an entry of a ref list) withholds the quote from
    every key, the admin key included, and ``alice_explain`` refuses, while an incidental one (under a free key, or in a
    sentence of a free text field) changes nothing. The object, a JSON text and a JSON array give the same answer.

    Mutations, each alone: do not decode a JSON string (the eight ``json`` cases that withhold are kept, and the cases that
    hold a marker in a quote fail); decode only an object (the four ``json array`` cases fail); stop the walk at the first
    JSON text (``break`` for the ``continue`` after the decode: ten cases fail); name every key of a decoded text as a
    reference, with every key renamed to ``sources`` in ``_keep_every_value`` (the incidental cases withhold).
    """

    cited = vault.capture_source()
    missing = str(uuid4())
    build, withheld, _storable_as_object = _MISSING_SHAPES[shape]
    ref = build(cited, missing)
    stored = {"object": ref, "json": json.dumps(ref), "json array": json.dumps([ref])}[encoding]
    memory_id, query, _body = _commit_with_refs(vault, [cited, stored], tag=_tag("missing", shape, encoding))  # type: ignore[list-item]
    for who in ("project", "admin"):
        for surface in ("review", "pack", "http_pack"):
            answer = getattr(vault, surface)(who, memory_id if surface == "review" else query)
            assert answer["is_error"] is False, (shape, encoding, who, surface)
            assert _holds_quote(answer) is (not withheld), (shape, encoding, who, surface)
        assert (vault.explain(who, memory_id)["is_error"] is True) is withheld, (shape, encoding, who)


@pytest.mark.parametrize(
    "damage",
    [
        lambda text: text[:-1],
        lambda text: text[:-1] + ",}",
        lambda text: text.replace('"quote"', "quote"),
        lambda text: "[" + text,
    ],
    ids=["truncated", "trailing comma", "unquoted key", "unclosed array"],
)
def test_a_text_that_does_not_decode_keeps_the_scan_of_its_text(vault: _Vault, damage: Callable[[str], str]) -> None:
    """The scan of the text is the fallback for a string that starts like JSON and is not. Such a ref is not a decoded
    value, so nothing says its quote is a quote, and the marker in it is read as it has been: named, missing, the quote is
    withheld and explain refuses. Over-withholding on a broken text is the behaviour of v0.20.0 and is kept.

    Mutation: skip the scan of a string that starts with ``{`` or ``[`` and does not decode (``continue`` before
    ``_ids_in_text``): the four cases keep the quote.
    """

    cited = vault.capture_source()
    nonexistent = str(uuid4())
    broken = damage(json.dumps(_ref_with_quote(cited, nonexistent)))
    assert _json_container(broken) is None
    memory_id, query, _body = _commit_with_refs(vault, [cited, broken], tag=_tag("broken", str(len(broken))))
    for surface in ("review", "pack", "http_pack"):
        answer = getattr(vault, surface)("project", memory_id if surface == "review" else query)
        assert answer["is_error"] is False, surface
        assert not _holds_quote(answer), surface
    assert vault.explain("project", memory_id)["is_error"] is True


# -- 2. the reading of a string, without a vault -----------------------------------------------------------------------


def _text_with_an_id(rng: random.Random, source_id: str) -> str:
    return rng.choice(
        (
            _quote_with_marker(source_id),
            f"copied from source: {source_id}",
            f"see alice://sources/{source_id}",
            source_id,
            f"{source_id} {source_id.upper()}",
            "source:" + source_id,
            "https://h.example/sources/" + source_id,
            "plain words",
        )
    )


def _with_text_fields(rng: random.Random, pool: list[str], node: object) -> object:
    """``node`` with, in about half of its objects, a ``quote`` or ``conversation_excerpt`` (in either case) that holds an id."""

    if isinstance(node, dict):
        out = {key: _with_text_fields(rng, pool, value) for key, value in node.items()}
        if rng.random() < 0.5:
            out[rng.choice(_TEXT_KEYS)] = _text_with_an_id(rng, rng.choice(pool))
        return out
    if isinstance(node, list):
        return [_with_text_fields(rng, pool, item) for item in node]
    return node


def _stringified_in_reference_positions(rng: random.Random, node: object, state: str = "ref") -> object:
    """``node`` with some of its objects and lists replaced by their JSON text, but only where the reader reads a string as
    a reference (the top, an entry of a list in such a position, and the value of a reference key), which is where the
    reader decodes it. The result must be read as ``node`` is."""

    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, value in node.items():
            lowered = key.lower()
            child = "ref" if lowered in _REFERENCE_KEYS or (state == "ref" and lowered in _OWN_ID_KEYS) else "other"
            out[key] = _stringified_in_reference_positions(rng, value, child)
        node = out
    elif isinstance(node, list):
        node = [_stringified_in_reference_positions(rng, item, state) for item in node]
    if isinstance(node, (dict, list)) and state == "ref" and rng.random() < 0.4:
        return json.dumps(node, indent=rng.choice((None, 1, 4)))
    return node


def _same_read(left: object, right: object) -> bool:
    a, b = cited_source_ids(left), cited_source_ids(right)
    return (a.named, a.incidental) == (b.named, b.incidental)


def test_a_json_text_is_read_as_the_value_it_decodes_to() -> None:
    """The independent judge: the read of an object. For 6,000 random refs (nested lists and objects under the keys the
    reader treats as references and under others, every spelling of an id, and in half of the objects a ``quote`` or
    ``conversation_excerpt``, in either case, that holds an id in a sentence, a URL, a marker or bare), the JSON text of the
    ref (compact, indented, padded, or the one entry of a list) is read exactly as the ref itself, named and incidental ids
    alike. A second form replaces random objects and lists by their JSON text at the positions where a string is read as a
    reference, which is JSON inside JSON, and is read as the original is. More than 2,000 refs hold a quote with an id, so
    the check cannot pass on refs that have none.

    Mutations, each alone: do not decode a JSON string, or scan its raw text as well (both read the ids in the quotes); decode
    only an object; stop the walk at the first JSON text (ids of the siblings go); decode a string in any position, with
    ``state == _REF`` dropped (a note that holds JSON is read as a reference when it is JSON text and not when it is an
    object). Each fails the equality.
    """

    rng = random.Random(545)
    pool = [*_new_ids(24), *(_zero_led_id(zeros) for zeros in (1, 1, 2, 3))]
    with_quote_ids = 0
    for _ in range(6_000):
        value = _with_text_fields(rng, pool, _random_ref(rng, pool))
        if not isinstance(value, (dict, list)):
            continue
        text = json.dumps(value)
        with_quote_ids += any(key in text for key in _TEXT_KEYS) and any(i in text for i in pool)
        assert _same_read(text, value), value
        assert _same_read(json.dumps(value, indent=2), value), value
        assert _same_read(" \n" + text + "\t ", value), value
        assert _same_read([text], [value]), value
        assert _same_read({"source_refs": [text]}, {"source_refs": [value]}), value
        stringified = _stringified_in_reference_positions(rng, value)
        assert _same_read(stringified, value), (stringified, value)
    assert with_quote_ids > 2_000


@pytest.mark.parametrize("key", _TEXT_KEYS)
@pytest.mark.parametrize(
    "text",
    [
        lambda stray: f"Log line: source:{stray} is a literal marker",
        lambda stray: f"source:{stray}",
        lambda stray: f"source: {stray}",
        lambda stray: stray,
        lambda stray: f"{stray} {str(uuid4())}",
        lambda stray: f"alice://sources/{stray}",
        lambda stray: f"SOURCE:{stray.upper()}",
    ],
    ids=["marker in a sentence", "marker", "marker and space", "bare id", "list of ids", "alice URL", "upper case"],
)
def test_the_quote_and_the_excerpt_of_a_decoded_ref_name_nothing(key: str, text: Callable[[str], str]) -> None:
    """Whatever a quote or an excerpt holds, an id in any spelling and any marker, it is text taken from a source and not a
    reference: in a JSON object, in an array of them, in JSON inside JSON, and in a quote that is itself JSON text. The
    cited id is named and nothing else is read.

    Mutations, each alone: do not decode a JSON string, or scan its raw text as well; skip the quote exclusion for a decoded
    value, with its text keys renamed to ``sources`` in ``_keep_every_value``; decode only an object; stop the walk at the
    first JSON text. Each fails all 28 cases or the ones that are arrays.
    """

    cited, stray = str(uuid4()), str(uuid4())
    ref = {"source_id": cited, key: text(stray)}
    for stored in (
        json.dumps(ref),
        json.dumps([ref]),
        json.dumps({"source_refs": [ref]}),
        json.dumps({"source_refs": [json.dumps(ref)]}),
        [json.dumps(ref)],
        {"sources": json.dumps([json.dumps(ref)])},
        json.dumps({"source_id": cited, key: json.dumps({"source_id": stray})}),
    ):
        cited_ids = cited_source_ids(stored)
        assert (set(cited_ids.named), set(cited_ids.incidental)) == ({cited}, set()), stored


def test_a_json_text_outside_a_reference_position_is_still_read_as_text() -> None:
    """Only a string read as a reference is decoded, as before: a JSON text under a free key (``origin``) is a note, and the
    ids in it are incidental, a quote's included. Decoding it too would change what a note names.

    Mutation: decode a string in a position that is not a reference too (``if state == _REF else None`` dropped from the
    decode): the id in the quote of the text is no longer read.
    """

    cited, noted = str(uuid4()), str(uuid4())
    text = json.dumps({"source_id": cited, "quote": f"source:{noted}"})
    cited_ids = cited_source_ids({"source_id": cited, "origin": text})
    assert cited_ids.named == frozenset({cited})
    assert cited_ids.incidental == frozenset({noted})


def test_a_key_that_is_repeated_in_a_json_text_keeps_every_value() -> None:
    """``json.loads`` keeps the last of a repeated key, so a decoded text would hide the first value, and the raw scan the
    reader made before found it. A repeated key now keeps every value (they are read as a list under that key), so a source id
    cannot be hidden from the reader behind a later key of the same name, and a repeated ``quote`` is skipped whole.

    Mutation: decode with plain ``json.loads`` (the first value of each repeated key is lost: the first id under
    ``source_id`` and the id under ``origin`` are not read). The cost test below holds the merge linear.
    """

    first, second, third, noted = (str(uuid4()) for _ in range(4))
    repeated = (
        f'{{"source_id": "{first}", "source_id": "{second}", "origin": "{third}", "origin": "plain", '
        f'"quote": "source:{noted}", "quote": "source:{noted}", "Quote": "x"}}'
    )
    assert json.loads(repeated)["source_id"] == second
    cited_ids = cited_source_ids(repeated)
    assert cited_ids.named == frozenset({first, second})
    assert cited_ids.incidental == frozenset({third})
    assert cited_source_ids([repeated]) == cited_ids


@pytest.mark.parametrize(
    "text",
    [
        lambda a, b: f'{{"source_id": "{a}", "quote": "see source:{b} here"',
        lambda a, b: f'["{a}", "see source:{b} here"',
        lambda a, b: f'{{"source_id": "{a}", "note": "see source:{b} here",}}',
        lambda a, b: f"{{source_id: {a}, note: 'see source:{b} here'}}",
        lambda a, b: f'[{a}, "see source:{b} here"]',
        lambda a, b: f'"see source:{b} here"',
        lambda a, b: f"7 see source:{b} here",
        lambda a, b: f'{{"source_id": "{a}"}} see source:{b} here',
    ],
    ids=["truncated object", "truncated array", "trailing comma", "unquoted keys", "bare id in an array", "a JSON string", "a number", "text after the value"],
)
def test_a_text_that_does_not_decode_to_an_object_or_a_list_keeps_the_scan(text: Callable[[str, str], str]) -> None:
    """The text is scanned as it was: ``source:<id>`` as a word of it is named. This includes a JSON string and a number (valid
    JSON that is not a container) and a value with text after it.

    Mutation: skip the scan of a string that starts with ``{`` or ``[`` and does not decode (``continue`` before
    ``_ids_in_text``): the six cases that start with one of them fail.
    """

    a, b = str(uuid4()), str(uuid4())
    raw = text(a, b)
    assert _json_container(raw) is None
    assert b in cited_source_ids(raw).named


def test_a_json_text_nested_deeper_than_the_decoder_goes_is_scanned_as_text() -> None:
    """``json.loads`` stops at the recursion limit of the interpreter with ``RecursionError``. That is the bound of the
    decode, as it is for the reader that came before: the text is then scanned as text, the id in it is read, and nothing is
    raised, so a request cannot make a reader fail with a deep ref. A ref nested to any depth as an object is read to the end
    by the walker, which keeps its own stack.

    Mutations, each alone: catch only ``ValueError`` in ``_json_container`` (``RecursionError`` reaches the reader); skip the
    scan of a string that starts with ``{`` or ``[`` and does not decode (the id of a text that is too deep is lost).
    """

    inner = str(uuid4())
    depth = 100_000
    deep_text = "[" * depth + json.dumps({"source_id": inner}) + "]" * depth
    assert _json_container(deep_text) is None
    assert inner in cited_source_ids(deep_text).every
    assert inner in cited_source_ids([deep_text]).every
    assert inner in cited_source_ids({"sources": deep_text}).every
    deep_object: object = {"source_id": inner}
    for _ in range(depth):
        deep_object = {"origin": deep_object}
    assert inner in cited_source_ids(deep_object).every


def _json_cost_cases(count: int) -> dict[str, tuple[str, set[str], set[str]]]:
    """``(ref, named, incidental)`` for JSON texts of ``count`` ids: in a quote (nothing is named), in a list (named), as the
    values of a wide object (four times as many keys, incidental), under one key that is repeated (named), and as a list of
    JSON texts."""

    ids = _new_ids(count)
    wide = _new_ids(4 * count)
    first = ids[0]
    return {
        "ids in a quote": (
            json.dumps({"source_id": first, "quote": " ".join("source: " + i for i in ids)}),
            {first},
            set(),
        ),
        "a list of refs": (json.dumps([{"source_id": i, "quote": "see source: x"} for i in ids]), set(ids), set()),
        "a wide object": (json.dumps({f"chunk{n}": i for n, i in enumerate(wide)}), set(), set(wide)),
        "a repeated key": ("{" + ", ".join(f'"source_id": "{i}"' for i in ids) + "}", set(ids), set()),
        "a list of JSON texts": (json.dumps([json.dumps({"source_id": i}) for i in ids]), set(ids), set()),
    }


@pytest.mark.parametrize("label", list(_json_cost_cases(1)))
def test_a_json_text_is_read_in_time_that_grows_with_its_length(label: str) -> None:
    """A JSON text of 8,000 ids is read in time that grows with its length (about as much as it is larger), and gives
    exactly the ids it holds. The decode and the merge of repeated keys are linear, and a text is not scanned twice.

    Mutations, each alone: build the merge of repeated keys with ``values = {**values, key: [*values.get(key, []), value]}``
    (the wide object takes quadratic time and fails on growth); decode with plain ``json.loads`` (the repeated key gives its
    last id only); do not decode (the quote's ids are read).
    """

    small, _named, _incidental = _json_cost_cases(_SMALL_IDS)[label]
    large, named, incidental = _json_cost_cases(_LARGE_IDS)[label]
    cited = cited_source_ids(large)
    assert (set(cited.named), set(cited.incidental)) == (named, incidental)
    assert_cost_grows_linearly(
        lambda: cited_source_ids(small),
        lambda: cited_source_ids(large),
        size_ratio=_LARGE_IDS // _SMALL_IDS,
        what=f"{label} ({len(large):,} characters)",
    )


# -- 3. the writer, against the reader, on refs that hold JSON text ----------------------------------------------------


def _json_shaped_strings(rng: random.Random, pool: list[str]) -> list[str]:
    """Strings that look like JSON and strings the writer reads that start with ``{`` or ``[``: a spelling of an id in
    braces, in brackets, with a JSON text around it, a truncated text, an empty object or list."""

    source_id = rng.choice(pool)
    spellings = (source_id, source_id.replace("-", ""), source_id.upper(), *_zero_led_spellings(source_id))
    spelled = rng.choice(spellings)
    text = json.dumps({"source_id": rng.choice(pool), "quote": f"source:{rng.choice(pool)}"})
    return [
        "{" + spelled + "}",
        "[" + spelled + "]",
        "[" + spelled,
        "{ " + spelled + " }",
        " {" + spelled + "} ",
        "source:{" + spelled + "}",
        "{}",
        "[]",
        "{" + " " * 32 + "}",
        " [ ] ",
        text[:-1],
        text + " " + spelled,
        spelled + " " + text,
        text,
        " " + text + "\n",
        json.dumps([text]),
        json.dumps(spelled),
    ]


def _inject_json_text(rng: random.Random, pool: list[str], node: object) -> object:
    """``node`` with some of its objects and lists replaced by their JSON text, at any position, and strings that look like
    JSON added to its lists and objects."""

    if isinstance(node, dict):
        out = {key: _inject_json_text(rng, pool, value) for key, value in node.items()}
        if rng.random() < 0.4:
            out[rng.choice(("source_id", "id", "ref", "source_ref", "origin", "sources"))] = rng.choice(
                _json_shaped_strings(rng, pool)
            )
        node = out
    elif isinstance(node, list):
        items = [_inject_json_text(rng, pool, item) for item in node]
        if rng.random() < 0.4:
            items.insert(rng.randint(0, len(items)), rng.choice(_json_shaped_strings(rng, pool)))
        node = items
    if isinstance(node, (dict, list)) and rng.random() < 0.4:
        return json.dumps(node)
    return node


def _strings_of(node: object) -> list[str]:
    found: list[str] = []
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def test_every_id_the_link_writer_reads_is_named_by_the_reader_in_refs_that_hold_json_text() -> None:
    """The reader is a superset of the writer (``source_uuids_in_ref``) however a ref is stored, JSON text included. 6,000
    random refs (the shapes the writer reads and many it does not, ids in every spelling the writer reads, quotes and
    excerpts that hold ids) have random objects and lists replaced by their JSON text, at any position, and JSON-shaped strings
    added: an id in braces or brackets, a text with an id after it, a truncated text, ``{}``, ``[]``. The reader names every id
    the writer reads. Skipping the scan of a text that decodes is safe only because the writer reads no id from one: the
    second assertion checks that on every string of every ref, and the third that the refs hold the writer's ids beside JSON
    text often enough to matter.

    Mutations, each alone, in ``cited_source_ids``: stop the walk at the first JSON text (``break`` for the ``continue`` that
    follows the decode: the ids of the siblings that were not read yet are lost); skip the scan of a string that starts with
    ``{`` or ``[`` and does not decode (the ids the writer reads in braces are lost).
    """

    rng = random.Random(546)
    pool = [*_new_ids(24), *(_zero_led_id(zeros) for zeros in (1, 1, 1, 2, 2, 3))]
    json_text_beside_a_written_id = 0
    for _ in range(6_000):
        value = _inject_json_text(rng, pool, _with_text_fields(rng, pool, _random_ref(rng, pool)))
        written = set(source_uuids_in_ref(value))
        assert written <= set(cited_source_ids(value).named), value
        strings = _strings_of(value)
        for text in strings:
            if _json_container(text) is not None:
                assert source_uuids_in_ref(text) == [], text
        json_text_beside_a_written_id += bool(written) and any(_json_container(text) is not None for text in strings)
    assert json_text_beside_a_written_id > 500


def test_a_json_text_is_never_an_id_the_writer_reads_in_any_spelling() -> None:
    """Every spelling of an id the writer reads (plain, upper case, no hyphens, braces, ``urn:uuid:``, a ``source:`` prefix, and
    for ids that start with ``0`` the spellings with whitespace, ``0x``, a sign or an underscore in place of the zeros), in braces
    and brackets and padded, is either an id the writer reads that is not JSON (and so is scanned and named) or JSON the writer
    reads nothing from. No string is both.

    This is the premise of the skip of the raw scan, and it fails if a string decodes to an object or a list and the writer
    reads an id from it. Mutation: skip the scan of a string that starts with ``{`` or ``[`` and does not decode (the
    braced spellings are no longer named).
    """

    pool = [*_new_ids(8), *(_zero_led_id(zeros) for zeros in (1, 2, 3))]
    checked = 0
    for source_id in pool:
        plain = (source_id, source_id.upper(), source_id.replace("-", ""), *_zero_led_spellings(source_id))
        for spelled in plain:
            for wrapper in (
                "{%s}",
                "[%s]",
                " {%s} ",
                "\n[%s]\n",
                "source:{%s}",
                "{%s",
                "%s}",
                "urn:uuid:%s",
                "{urn:uuid:%s}",
                '{"%s"}',
                '["%s"]',
                '{"source_id": "%s"}',
                '["%s", "%s"]',
            ):
                text = wrapper.replace("%s", spelled)
                written = source_uuids_in_ref(text)
                if _json_container(text) is not None:
                    assert written == [], text
                else:
                    assert set(written) <= set(cited_source_ids(text).named), text
                checked += 1
    assert checked > 500


# -- 4. the reader and the audit, on a stub store -----------------------------------------------------------------------


@pytest.mark.parametrize("encoding", list(_ENCODINGS))
def test_a_memory_whose_ref_holds_a_marker_in_a_quote_is_the_stored_row_for_a_caller_who_may_read_the_source(
    encoding: str,
) -> None:
    """The reader of the review, the revision and the audit that ``alice_explain`` judges, for a memory whose refs hold the
    cited source and a ref (in each encoding) with a marker in its quote. A caller who may read the source gets the stored
    row, the link keeps its quote, the revision is the stored revision, and the audit names the source and nothing else.

    Mutation: do not decode a JSON string, or scan its raw text as well (the seven JSON encodings fail: the row is a copy,
    the quote is gone, the audit names the marker).
    """

    store = _MemoryStore()
    readable = store.add_source()
    stray = str(uuid4())
    stored = _ENCODINGS[encoding](_ref_with_quote(readable, stray))
    refs = [readable, stored]
    memory_id = str(uuid4())
    row = _row(memory_id, refs=refs)
    store.memories[memory_id] = row
    link = store.add_link(memory_id, readable)
    reader = _reader(store)
    revision = _revision(memory_id, refs)
    assert reader.memory(row) is row
    assert reader.links(memory_id)[0] is link
    assert reader.revision(revision) is revision
    audit = cited_source_ids_in_memory_audit({"memory": row, "revisions": [revision]})
    assert audit.named == frozenset({readable})
    assert audit.incidental == frozenset()
