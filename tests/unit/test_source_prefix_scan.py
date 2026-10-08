"""The ``source:`` prefixes of a ref are read in linear time and exactly as the earlier pattern read them.

``_source_prefixes_end`` replaced a match of ``(?:\\s*source:)*`` (ignoring case) at the start of the text. The reader
of saved quotes and the link writer depend on where the prefixes end, so the loop must give the same end for every
text, and it must do so in time linear in the text however much whitespace it holds.

Mutations, each alone, in ``vnext_source_fence.py``: drop ``re.IGNORECASE`` from ``_SOURCE_PREFIX`` (the case rows and
the random texts fail); skip whitespace after each prefix as well (the rows with whitespace after the last prefix
fail); stop after the first prefix (the repeated-prefix rows fail); test ``== " "`` instead of ``isspace()`` (the tab,
newline and Unicode space rows fail).
"""

from __future__ import annotations

import random
import re
import sys
import time

import pytest

from alicebot_api.vnext_source_fence import _source_prefixes_end

_EARLIER = re.compile(r"(?:\s*source:)*", re.IGNORECASE)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "source:",
        "Source:",
        "SOURCE:",
        "ſource:",  # LATIN SMALL LETTER LONG S matches ``s`` when case is ignored
        "source:source:",
        "source: source:\tsource:\n",
        "  source:  x",
        "source: abc",
        " source:　source:",
        "\x1csource:",
        "sourc:",
        "source",
        " source",
        "x source:",
        "source:source",
        ":source:",
        "source:" * 50 + " " * 50,
    ],
)
def test_the_prefixes_end_where_the_earlier_pattern_ended(text: str) -> None:
    assert _source_prefixes_end(text) == _EARLIER.match(text).end()  # type: ignore[union-attr]


def test_random_texts_end_where_the_earlier_pattern_ended() -> None:
    pieces = ["source:", "SOURCE:", "Source:", "ſource:", "sourc", "e", ":", " ", "\t", "\n", " ", " ", "\x1c", "x", "0", "a"]
    rng = random.Random(20261008)
    for _ in range(20000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 12)))
        assert _source_prefixes_end(text) == _EARLIER.match(text).end(), repr(text)  # type: ignore[union-attr]


def test_isspace_is_the_whitespace_the_earlier_pattern_skipped() -> None:
    space = re.compile(r"\s")
    for code_point in range(sys.maxunicode + 1):
        character = chr(code_point)
        assert character.isspace() == (space.match(character) is not None), hex(code_point)


@pytest.mark.parametrize(
    "text",
    [
        " " * 2_000_000 + "x",
        "\t\n" * 1_000_000 + "source",
        "source:" * 200_000 + " " * 1_000_000 + "x",
        " source:" * 250_000,
    ],
)
def test_long_whitespace_is_read_in_linear_time(text: str) -> None:
    started = time.perf_counter()
    end = _source_prefixes_end(text)
    elapsed = time.perf_counter() - started
    assert end == _EARLIER.match(text).end()  # type: ignore[union-attr]
    assert elapsed < 5.0, elapsed
