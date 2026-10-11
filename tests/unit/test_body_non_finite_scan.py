"""The request body check for a number that is not finite never misses one, and reads an ordinary body at memory speed.

``reject_lone_surrogate_json_body`` answers HTTP 422 (type ``finite_number``) for a body that holds ``NaN``, an infinity or
a float that overflows (``1e999``). Decoding every body to find out would double the cost of every request, so
``may_hold_non_finite`` answers first from the bytes: ``False`` is exact (the body cannot hold one), ``True`` sends the body
to the decoder. The first version was one regular expression and read a body at 47 MB/s (75 ms for 3.9 MB). It is now a few
searches with ``in``, which run at memory speed, over the body and over the body written as a shape.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* the digit rule drops to a run of 300 digits (the old rule): ``test_a_body_that_decodes_to_an_infinity_is_never_cleared``.
* the exponent rule asks for four digits: the same test.
* the token check reads a number inside a hex id as the number: ``test_a_hex_id_that_looks_like_an_exponent_is_not_decoded``.
* the token check cuts the sign off a negative number: the same fuzz.
* the scan is a regular expression again: ``test_an_ordinary_body_is_read_much_faster_than_a_regular_expression_reads_it``.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from uuid import uuid4

import pytest

from alicebot_api.lone_surrogates import body_non_finite_location, may_hold_non_finite


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
    sign = rng.choice(("", "", "-"))
    integer = str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(rng.choice((0, 0, 1, 5, 50, 150, 198, 199, 200, 250, 298, 299, 300, 320))))
    fraction = rng.choice(("", "", ".5", ".000123"))
    exponent = rng.choice(("", "", "e5", "E+10", "e-5", "e11", "e+100", "e111", "e308", "e309", "E400", "e+999", "e0400", "e-400", "e12345"))
    return f"{sign}{integer}{fraction}{exponent}"


def _body(rng: random.Random, number: str) -> bytes:
    words = " ".join(rng.choice(("the", "lantern", "cafe", "edge", "dead", "beef")) for _ in range(rng.randint(0, 6)))
    ident = str(uuid4())
    shape = rng.choice((
        '{"a": %s}', '[%s, 1]', '{"a": {"b": [1, %s]}}', '{"text": "%s", "n": 1}', '{"text": "' + words + ' %s end"}',
        '{"id": "' + ident + '", "v": [%s]}'))
    return (shape % number).encode()


def test_a_body_that_decodes_to_an_infinity_is_never_cleared() -> None:
    rng = random.Random(20261012)
    infinite = cleared = 0
    for _ in range(12000):
        raw = _body(rng, _number(rng))
        truth = _infinite(raw)
        answer = may_hold_non_finite(raw)
        assert answer or not truth, raw[:120]
        infinite += truth
        cleared += (not answer)
        assert (body_non_finite_location(raw) is not None) == truth, raw[:120]
    # A fuzz that never produced an infinity, or never cleared a body, would agree with anything.
    assert infinite > 1500 and cleared > 2000, (infinite, cleared)


@pytest.mark.parametrize(
    "number",
    ["1e400", "-1e400", "1E+400", "1.5e999", "1e0400", "0.5e+310", "1" + "0" * 309 + ".5", "1" + "0" * 250 + "e60", "1" + "0" * 199 + "e111"],
)
def test_the_numbers_that_overflow_are_found(number: str) -> None:
    for shape in ('{"a": %s}', "[%s]", '{"a": [1, {"b": %s}]}'):
        raw = (shape % number).encode()
        assert _infinite(raw), raw[:80]
        assert may_hold_non_finite(raw), raw[:80]


@pytest.mark.parametrize("word", [b"NaN", b"Infinity", b"-Infinity"])
def test_the_words_and_a_wide_encoding_are_found(word: bytes) -> None:
    assert may_hold_non_finite(b'{"a": ' + word + b"}")
    assert may_hold_non_finite('{"a": 1}'.encode("utf-16"))
    assert may_hold_non_finite('{"a": 1}'.encode("utf-32"))


def test_a_hex_id_that_looks_like_an_exponent_is_not_decoded() -> None:
    """A uuid has the shape of an exponent about one time in four ("...3e450..."): it is not a number, because a letter or a
    sign stands before it, so a body full of ids is not sent to the decoder for that."""

    ids = ["8c1e350a-5e3500abc", "x1e999y", "a-1e400", "id_1e400", "1e400a", "..5e350..", "1e400000-aaaa", "5e350000-5e350000"]
    raw = json.dumps({"ids": ids, "more": [str(uuid4()) for _ in range(500)]}).encode()
    assert any(each in raw.translate(bytes(range(256))) for each in (b"e350", b"e999", b"e400"))
    assert not _infinite(raw)
    assert may_hold_non_finite(raw) is False


def test_a_number_beside_a_word_is_still_found() -> None:
    assert may_hold_non_finite(b'{"note": "see 1e400", "n": 1e400}') is True


def _prose(size: int) -> bytes:
    rng = random.Random(5)
    words = "the lantern budget review meeting Alice Marlow notes decided cobalt door opens on Monday vendor contract signed".split()
    text = " ".join(rng.choice(words) for _ in range(size // 6))
    return json.dumps({"user_id": str(uuid4()), "canonical_text": text, "source_refs": [{"source_id": str(uuid4()), "quote": "q"}]}).encode()


OLD = re.compile(rb"NaN|Infinity|[0-9][eE][+-]?[0-9]{3,}|[0-9]{300,}|\x00")


@pytest.mark.parametrize("size", (390_000, 3_900_000))
def test_an_ordinary_body_is_read_much_faster_than_a_regular_expression_reads_it(size: int) -> None:
    raw = _prose(size)

    def best(call) -> float:
        times = []
        for _ in range(4):
            started = time.perf_counter()
            call(raw)
            times.append(time.perf_counter() - started)
        return min(times)

    new, old = best(may_hold_non_finite), best(OLD.search)
    # Measured 3 to 8 times faster (a body of prose: about 400 MB/s against 52 MB/s); half is a margin for a slow machine.
    assert new < old * 0.5, (size, new, old)
