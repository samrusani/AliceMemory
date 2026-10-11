"""The two halves of ``alicebot_api.json_bounds``: what a write door accepts, and a decoder a stored row cannot overflow.

The write half (``first_json_problem``, ``refuse_unstorable_json``, ``loads_caller_json``) refuses JSON nested more than 64
levels and any number that is not finite. The read half (``loads_bounded``) decodes stored text and never raises
``RecursionError``: text nested no deeper than 256 levels comes back as ``json.loads`` returns it, and the part of a
deeper text below that depth is replaced by one string.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``first_json_problem`` compares ``depth`` with ``limit + 1``: ``test_depth_is_counted_as_levels_of_arrays_and_objects``.
* ``first_json_problem`` stops looking at a float inside a list: ``test_a_number_that_is_not_finite_is_found_wherever_it_stands``.
* ``first_json_problem`` recurses instead of keeping a stack: ``test_a_value_nested_a_million_levels_is_judged_not_recursed_into``.
* ``cut_deep_json`` reads a bracket inside a string as a bracket, or ``text_nests_deeper_than`` stops blanking the strings:
  ``test_a_bracket_inside_a_string_is_text``.
* ``loads_bounded`` cuts at ``limit - 1``: ``test_text_at_the_read_limit_is_decoded_whole``.
* ``loads_bounded`` skips the cut: ``test_text_nested_past_the_read_limit_is_decoded_with_the_deep_part_replaced``.
* the string pattern of ``_STRING`` loses its possessive quantifiers: ``test_a_long_string_of_escapes_costs_one_pass``.
* ``cut_deep_json`` cuts one level early: ``test_the_cut_text_is_valid_json_with_one_placeholder_per_deep_branch``.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from alicebot_api import json_bounds
from alicebot_api.json_bounds import (
    DEEP_PLACEHOLDER,
    MAX_READ_JSON_DEPTH,
    MAX_STORED_JSON_DEPTH,
    JsonNotFinite,
    JsonTooDeep,
    cut_deep_json,
    first_json_problem,
    loads_bounded,
    loads_caller_json,
    refuse_unstorable_json,
    text_nests_deeper_than,
)


def _nested(depth: int, leaf: object = "x") -> object:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _text(depth: int, leaf: str = "1") -> str:
    return "[" * depth + leaf + "]" * depth


def _depth_of(value: object) -> int:
    depth = 0
    while isinstance(value, list) and value:
        depth += 1
        value = value[0]
    return depth


def test_the_limits_are_the_documented_numbers() -> None:
    import alicebot_api.request_limits as request_limits

    assert MAX_STORED_JSON_DEPTH == 64
    assert MAX_READ_JSON_DEPTH == request_limits.MAX_JSON_NESTING == 256


def test_depth_is_counted_as_levels_of_arrays_and_objects() -> None:
    assert first_json_problem("x") is None
    assert first_json_problem(1.5) is None
    assert first_json_problem([]) is None
    assert first_json_problem(_nested(MAX_STORED_JSON_DEPTH)) is None
    assert first_json_problem(_nested(MAX_STORED_JSON_DEPTH + 1)) == json_bounds.TOO_DEEP
    # An object counts as a level too, and the deepest branch decides.
    objects = {"a": {"b": {"c": 1}}}
    assert first_json_problem(objects, limit=3) is None
    assert first_json_problem(objects, limit=2) == json_bounds.TOO_DEEP
    mixed = {"shallow": [1, 2], "deep": [[{"k": [[1]]}]]}
    assert first_json_problem(mixed, limit=6) is None
    assert first_json_problem(mixed, limit=5) == json_bounds.TOO_DEEP
    assert first_json_problem(_nested(3), limit=3) is None and first_json_problem(_nested(4), limit=3) == json_bounds.TOO_DEEP


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), float("-inf")))
def test_a_number_that_is_not_finite_is_found_wherever_it_stands(bad: float) -> None:
    for value in (bad, [bad], {"a": bad}, {"a": [1, {"b": [2, bad]}]}, (1, bad), _nested(10, bad)):
        assert first_json_problem(value) == json_bounds.NOT_FINITE, value
    assert first_json_problem({"a": [1.5, -2.5e300, 10**400, True, None, "NaN"]}) is None


def test_a_value_nested_a_million_levels_is_judged_not_recursed_into() -> None:
    value: object = "x"
    for _ in range(1_000_000):
        value = [value]
    assert first_json_problem(value) == json_bounds.TOO_DEEP
    loop: list[object] = []
    loop.append(loop)
    assert first_json_problem(loop) == json_bounds.TOO_DEEP


def test_refusal_names_the_field_and_never_the_value() -> None:
    with pytest.raises(JsonTooDeep) as deep:
        refuse_unstorable_json(_nested(80, "secret-value-sentinel"), name="source_refs")
    assert str(deep.value) == "source_refs must nest at most 64 levels of arrays and objects"
    assert "sentinel" not in str(deep.value)
    with pytest.raises(JsonNotFinite) as bad:
        refuse_unstorable_json({"score": float("nan")}, name="provenance")
    assert str(bad.value) == "provenance must not hold NaN, Infinity or a number too large for a float"
    refuse_unstorable_json({"score": 0.5, "refs": [_nested(60)]}, name="ok")


# The decoder.


@pytest.mark.parametrize(
    "document",
    (
        "{}",
        "[]",
        '{"a": [1, 2.5, "x", null, true], "b": {"c": {"d": []}}}',
        '{"text": "brackets [[[ in a string ]]] and {{{ too", "n": 3}',
        '"a bare string"',
        "42",
        _text(MAX_READ_JSON_DEPTH),
        '{"k": ' + _text(MAX_READ_JSON_DEPTH - 1) + "}",
    ),
)
def test_text_at_the_read_limit_is_decoded_whole(document: str) -> None:
    assert loads_bounded(document) == json.loads(document)
    assert loads_bounded(document.encode()) == json.loads(document)


def test_text_nested_past_the_read_limit_is_decoded_with_the_deep_part_replaced() -> None:
    for depth in (MAX_READ_JSON_DEPTH + 1, 900, 5_000, 200_000):
        decoded = loads_bounded(_text(depth))
        assert _depth_of(decoded) == MAX_READ_JSON_DEPTH, depth
        leaf: object = decoded
        for _ in range(MAX_READ_JSON_DEPTH):
            leaf = leaf[0]  # type: ignore[index]
        assert leaf == DEEP_PLACEHOLDER


def test_only_the_deep_branch_is_cut_and_its_neighbours_are_kept() -> None:
    deep = _text(MAX_READ_JSON_DEPTH + 40)
    document = '{"keep": [1, 2, {"also": "kept"}], "deep": ' + deep + ', "after": {"x": [3]}}'
    decoded = loads_bounded(document)
    assert decoded["keep"] == [1, 2, {"also": "kept"}]  # type: ignore[index]
    assert decoded["after"] == {"x": [3]}  # type: ignore[index]
    assert _depth_of(decoded["deep"]) == MAX_READ_JSON_DEPTH - 1  # type: ignore[index]
    # Two deep branches are each cut, and the text between them survives.
    two = "[" + deep + "," + '"between"' + "," + deep + "]"
    decoded = loads_bounded(two)
    assert decoded[1] == "between"  # type: ignore[index]
    assert _depth_of(decoded[0]) == MAX_READ_JSON_DEPTH - 1  # type: ignore[index]
    assert _depth_of(decoded[2]) == MAX_READ_JSON_DEPTH - 1  # type: ignore[index]


def test_a_bracket_inside_a_string_is_text() -> None:
    noise = '"' + "[" * 600 + "{" * 600 + '"'
    document = '{"note": ' + noise + ', "list": [1, [2]]}'
    assert not text_nests_deeper_than(document, MAX_READ_JSON_DEPTH)
    assert loads_bounded(document) == json.loads(document)
    escaped = '{"note": "a \\" [[[[[[[[ b", "z": [[1]]}'
    assert loads_bounded(escaped, limit=3) == json.loads(escaped)
    assert cut_deep_json(escaped, 3) == escaped
    assert loads_bounded(escaped, limit=2)["z"] == [DEEP_PLACEHOLDER]  # type: ignore[index]


def test_text_that_is_not_json_is_refused_as_json_refuses_it() -> None:
    for bad in ("", "[1,", "{'a': 1}", "[" * 300, "[1, 2" + "]" * 300):
        with pytest.raises(json.JSONDecodeError):
            loads_bounded(bad)
        with pytest.raises(json.JSONDecodeError):
            json.loads(bad)
    # What is wrong only below the cut is cut away with it: the rest is decoded.
    assert loads_bounded("[" * 300 + "x" + "]" * 300) is not None


def test_the_cut_text_is_valid_json_with_one_placeholder_per_deep_branch() -> None:
    cut = cut_deep_json(_text(300), 10)
    assert cut == "[" * 10 + json.dumps(DEEP_PLACEHOLDER) + "]" * 10
    assert json.loads(cut)  # decodes


def test_a_long_string_of_escapes_costs_one_pass() -> None:
    """A text that is mostly escaped quotes, with enough brackets to need the scan, must not cost the square of its length."""

    body = '\\"' * 400_000
    document = '{"s": "' + body + '", "deep": ' + _text(300) + "}"
    started = time.perf_counter()
    assert text_nests_deeper_than(document, MAX_READ_JSON_DEPTH)
    decoded = loads_bounded(document)
    elapsed = time.perf_counter() - started
    assert decoded["s"].count('"') == 400_000  # type: ignore[index]
    assert elapsed < 3.0, elapsed
    unterminated = '{"s": "' + body + _text(400)
    started = time.perf_counter()
    text_nests_deeper_than(unterminated, MAX_READ_JSON_DEPTH)
    assert time.perf_counter() - started < 3.0


def test_the_decoder_reads_bytes_and_memoryview_as_the_database_driver_hands_them() -> None:
    text = '{"a": ' + _text(MAX_READ_JSON_DEPTH + 3) + "}"
    from_bytes = loads_bounded(text.encode())
    assert from_bytes == loads_bounded(text)
    assert from_bytes == loads_bounded(bytearray(text.encode())) == loads_bounded(memoryview(text.encode()))
    with pytest.raises(TypeError):
        loads_bounded(12345)  # type: ignore[arg-type]


# The text a caller types or supplies in a file.


@pytest.mark.parametrize(
    "text",
    ('{"a": NaN}', "[Infinity]", '{"a": {"b": -Infinity}}', '{"a": 1e999}', "[-1e999]", "[1e400]", "[1" + "0" * 400 + ".5]"),
)
def test_caller_text_with_a_number_that_is_not_finite_is_refused(text: str) -> None:
    with pytest.raises(JsonNotFinite) as refused:
        loads_caller_json(text, name="--provenance-json")
    assert str(refused.value).startswith("--provenance-json must not hold")


def test_caller_text_nested_past_the_write_limit_is_refused_before_it_is_decoded() -> None:
    for depth in (MAX_STORED_JSON_DEPTH + 1, 900, 200_000):
        with pytest.raises(JsonTooDeep):
            loads_caller_json(_text(depth), name="--body-json")
    assert loads_caller_json(_text(MAX_STORED_JSON_DEPTH), name="--body-json") is not None


def test_caller_text_that_is_fine_decodes_as_json_does() -> None:
    text = '{"a": [1, 2.5, -3e5, "NaN", {"b": null}], "c": 10000000000000000000000}'
    assert loads_caller_json(text, name="x") == json.loads(text)
    with pytest.raises(json.JSONDecodeError):
        loads_caller_json("{not json", name="x")
    assert math.isfinite(loads_caller_json("1.5", name="x"))  # type: ignore[arg-type]
