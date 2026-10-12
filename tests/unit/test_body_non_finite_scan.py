"""The request body check for a number that is not finite never misses one, and reads an ordinary body at decoder speed.

``reject_lone_surrogate_json_body`` answers HTTP 422 (type ``finite_number``) for a body that holds ``NaN``, an infinity or
a float that overflows (``1e999``). The first version searched the bytes with one regular expression and read a body at 47
MB/s (75 ms for 3.9 MB of text), and decoded and walked a body that matched, which a body full of uuids often did, because
the hex digits of an id can spell an exponent. ``body_non_finite_location`` now decodes the body once, in C, with two hooks
that raise at the first number that is not finite (``parse_constant`` for the three words, ``parse_float`` for every float).
That costs about what ``json.loads`` costs, and only a body that holds one is decoded again, without the hooks, for the walk
that says where.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* the ``parse_float`` hook is dropped: ``test_a_body_that_decodes_to_an_infinity_is_never_cleared`` (a float that overflows).
* the ``parse_constant`` hook is dropped: the same test (the three words).
* the float hook does not test the number: the same test.
* the place of the number is not looked up (the walk is skipped): ``test_the_numbers_that_are_not_finite_are_found_and_placed``.
* the body is searched with a regular expression first (the old scan): ``test_an_ordinary_body_is_read_much_faster_than_the_old_scan_read_it``.
"""

from __future__ import annotations

import json
import math
import random
import re
import time

import pytest

from alicebot_api.lone_surrogates import body_non_finite_location


def _infinite(raw: bytes) -> bool:
    """What the route would receive: whether the decoder gives any infinity or NaN."""

    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    stack = [decoded]
    while stack:
        value = stack.pop()
        if isinstance(value, float) and not math.isfinite(value):
            return True
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def _number(rng: random.Random) -> str:
    if rng.random() < 0.06:
        return rng.choice(("NaN", "Infinity", "-Infinity"))
    sign = rng.choice(("", "", "-"))
    integer = str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(rng.choice((0, 0, 1, 5, 50, 150, 198, 199, 200, 250, 298, 299, 300, 320))))
    fraction = rng.choice(("", "", ".5", ".000123"))
    exponent = rng.choice(("", "", "e5", "E+10", "e-5", "e11", "e+100", "e111", "e308", "e309", "E400", "e+999", "e0400", "e-400", "e12345"))
    return f"{sign}{integer}{fraction}{exponent}"


def _body(rng: random.Random, number: str) -> bytes:
    words = " ".join(rng.choice(("the", "lantern", "cafe", "edge", "dead", "beef")) for _ in range(rng.randint(0, 6)))
    ident = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    shape = rng.choice((
        '{"a": %s}', '[%s, 1]', '{"a": {"b": [1, %s]}}', '{"text": "%s", "n": 1}', '{"text": "' + words + ' %s end"}',
        '{"id": "' + ident + '", "v": [%s]}', '{"a":%s}', '[\n  %s\n]'))
    return (shape % number).encode()


def test_a_body_that_decodes_to_an_infinity_is_never_cleared() -> None:
    rng = random.Random(20261012)
    infinite = clean = 0
    for _ in range(12000):
        raw = _body(rng, _number(rng))
        truth = _infinite(raw)
        assert (body_non_finite_location(raw) is not None) == truth, raw[:120]
        infinite += truth
        clean += not truth
    # A fuzz that never produced an infinity, or never a clean body, would agree with anything.
    assert infinite > 1500 and clean > 2000, (infinite, clean)


@pytest.mark.parametrize(
    "number",
    ["1e400", "-1e400", "1E+400", "1.5e999", "1e0400", "0.5e+310", "1" + "0" * 309 + ".5", "1" + "0" * 250 + "e60", "1" + "0" * 199 + "e111",
     "NaN", "Infinity", "-Infinity"],
)
def test_the_numbers_that_are_not_finite_are_found_and_placed(number: str) -> None:
    assert body_non_finite_location(('{"a": %s}' % number).encode()) == ("a",)
    assert body_non_finite_location(("[%s]" % number).encode()) == (0,)
    assert body_non_finite_location(('{"a": [1, {"b": %s}]}' % number).encode()) == ("a", 1, "b")


def test_a_wide_encoding_is_read_as_the_framework_reads_it() -> None:
    assert body_non_finite_location('{"a": NaN}'.encode("utf-16")) == ("a",)
    assert body_non_finite_location('{"a": 1e999}'.encode("utf-32")) == ("a",)
    assert body_non_finite_location('{"a": 1}'.encode("utf-16")) is None


def test_a_hex_id_and_a_string_that_look_like_numbers_are_not_numbers() -> None:
    raw = json.dumps({"ids": ["8c1e350a-5e3500abc", "x1e999y", "a-1e400", "id_1e400", "1e400a", "..5e350..", "1e400000-aaaa"],
                      "text": "see 1e400 and NaN and Infinity in the note"}).encode()
    assert not _infinite(raw)
    assert body_non_finite_location(raw) is None


@pytest.mark.parametrize("raw", [b"", b"not json", b"[NaN", b'{"a": 1e400', b"[1, 2,", b"\xff\xfe", b"[" * 200_000, b"1" * 5000])
def test_a_body_that_is_not_json_is_left_to_the_framework(raw: bytes) -> None:
    assert body_non_finite_location(raw) is None


def test_a_deeply_nested_body_does_not_overflow_the_scan() -> None:
    assert body_non_finite_location(b"[" * 100_000 + b"1e400" + b"]" * 100_000) is None
    assert body_non_finite_location(b"[" * 3000 + b"1e400" + b"]" * 3000) == (0,) * 3000


def _text_body(size: int) -> bytes:
    rng = random.Random(5)
    words = "the lantern budget review meeting Alice Marlow notes decided cobalt door opens on Monday vendor contract signed".split()
    text = " ".join(rng.choice(words) for _ in range(size // 6))
    # Ids with no ``e`` in them: the old scan matched ``[0-9]e[0-9]{3}``, which most random ids hold, and then decoded.
    return json.dumps({"user_id": "a1b2c3d4-0f9a-4c7b-8d6a-0b1c2d3f4a5b", "canonical_text": text,
                       "source_refs": [{"source_id": "0a1b2c3d-4f5a-4b6c-9d7a-1b2c3d4f5a6b", "quote": "q"}]}).encode()


OLD_SCAN = re.compile(rb"NaN|Infinity|[0-9][eE][+-]?[0-9]{3,}|[0-9]{300,}|\x00")


@pytest.mark.parametrize("size", (390_000, 3_900_000))
def test_an_ordinary_body_is_read_much_faster_than_the_old_scan_read_it(size: int) -> None:
    raw = _text_body(size)

    def best(call) -> float:
        times = []
        for _ in range(4):
            started = time.perf_counter()
            call(raw)
            times.append(time.perf_counter() - started)
        return min(times)

    new, old = best(body_non_finite_location), best(OLD_SCAN.search)
    # Measured 7 to 25 times faster (390 KB of text: 0.3 ms against 7.4 ms; 3.9 MB: 3 ms against 74 ms); half is a margin
    # for a slow machine.
    assert new < old * 0.5, (size, new, old)
