"""The text pass looks for every id at once, and finds what looking for each id one by one finds.

``_add_text_hits`` answers, for one lower case text, which of the probed ids the text may cite. It used to ask ``digits in
text`` for each id, so a pass over the edges (or the memories) of a vault cost the number of rows times the number of ids:
a folder re-import of 800 notes over 106,000 edges read every edge 800 times. ``_Probes.find`` now reads each run of
hexadecimal digits of the text once and looks up each window of it in a table of the ids.

These tests hold the new pass to the old one (kept here as ``linear_hits``) over random texts that write ids the way the
product's data does, and to the cost: reading 5,000 ids takes about what reading 25 does. Up to ``_LINEAR_PROBES`` ids the
pass asks for each in turn, which is cheaper than the table, so the tests that hold the table use more ids than that.

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``_Probes.find`` reads the first window of a run only: ``test_the_indexed_pass_finds_what_the_linear_pass_finds``.
* ``_Probes.find`` treats a run longer than ``_LONG_RUN`` as no run: ``test_a_long_run_of_hex_digits_is_searched_as_a_whole``.
* ``_Probes.find`` leaves out the ids with short digits: ``test_the_indexed_pass_finds_what_the_linear_pass_finds``.
* ``_Probes.find`` asks each id in turn whatever their number (the old loop): ``test_the_cost_does_not_grow_with_the_number_of_ids``.
* the table is never used (``_LINEAR_PROBES`` made huge): the same tests, which hold more ids than the plain search takes.
"""

from __future__ import annotations

import random
import time
from uuid import UUID, uuid4

from alicebot_api.vnext_stores.sqlite import source_retirement
from alicebot_api.vnext_stores.sqlite.source_retirement import (
    _LINEAR_PROBES,
    _LONG_RUN,
    _Probes,
    _add_text_hits,
    _citation_probe,
    _in_ascii_digits,
    _without_ignored,
)


def linear_hits(text, probes, hits):
    """``_add_text_hits`` as it was written before ``find``: one ``in`` per id."""

    escaped = "\\u" in text
    if escaped and source_retirement._ESCAPED_ASCII.search(text):
        hits |= probes.every_text
        return
    stripped = _without_ignored(text)
    for index, digits in probes.digits:
        if digits in stripped:
            hits.add(index)
    if len(hits) < len(probes.every) and (escaped or not text.isascii()):
        read = _in_ascii_digits(text)
        if read is not None:
            stripped = _without_ignored(read)
            for index, digits in probes.digits:
                if digits in stripped:
                    hits.add(index)


SPELLINGS = (
    lambda x: x,
    lambda x: x.upper(),
    lambda x: x.replace("-", ""),
    lambda x: "{" + x + "}",
    lambda x: "urn:uuid:" + x,
    lambda x: "source:" + x,
    lambda x: x.replace("-", "_"),
    lambda x: x[:8] + "-" + x[9:].replace("-", ""),
    lambda x: x + "ab",
    lambda x: "f" + x,
    lambda x: x[:20],
    lambda x: x[5:],
)
WORDS = ("the", "lantern", "cafe", "dead", "beef", "added", "facade", "budget", "review", "note", "ä", "é", "中")
OTHER_DIGITS = {"0": "٠", "1": "١", "2": "٢", "3": "٣", "4": "٤", "5": "٥", "6": "٦", "7": "٧", "8": "٨", "9": "٩"}


def _ids(rng, count):
    ids = []
    for _ in range(count):
        kind = rng.random()
        if kind < 0.1:
            ids.append("0" * rng.randint(1, 20) + uuid4().hex[20:])  # many leading zeros: short digits
        elif kind < 0.2:
            ids.append(uuid4().hex[:rng.randint(4, 15)])  # short digit strings
        elif kind < 0.25 and ids:
            ids.append(rng.choice(ids))  # the same id given twice
        else:
            ids.append(str(uuid4()))
    return ids


def _probes_for(ids):
    sources = []
    for item in ids:
        try:
            digits = UUID(item).hex.lstrip("0") if len(item.replace("-", "")) == 32 else item.replace("-", "").lstrip("0")
        except ValueError:
            digits = None
        sources.append((item, digits or None))
    sources.append(("not an id", None))
    return _Probes(sources)


def _text(rng, ids):
    pieces = []
    for _ in range(rng.randint(0, 8)):
        kind = rng.random()
        if kind < 0.35 and ids:
            pieces.append(rng.choice(SPELLINGS)(rng.choice(ids)))
        elif kind < 0.5:
            pieces.append(uuid4().hex + uuid4().hex)  # a 64 digit hash
        elif kind < 0.55 and ids:
            pieces.append("".join(rng.choice("0123456789abcdef") for _ in range(rng.randint(1, 40))) + rng.choice(ids).replace("-", ""))
        elif kind < 0.6 and ids:
            chosen = rng.choice(ids)
            pieces.append("".join(OTHER_DIGITS.get(char, char) for char in chosen))
        elif kind < 0.63:
            pieces.append("\\u00" + rng.choice(("61", "2d", "09", "e9")))
        elif kind < 0.66 and ids:
            pieces.append("x" * rng.randint(0, 3) + rng.choice(ids).replace("-", "") * 2)
        else:
            pieces.append(rng.choice(WORDS))
    return rng.choice((" ", "\n", "", "-", "_")).join(pieces).lower()


def test_the_indexed_pass_finds_what_the_linear_pass_finds() -> None:
    rng = random.Random(20261011)
    checked = 0
    for _ in range(6000):
        ids = _ids(rng, rng.randint(0, 70))
        probes = _probes_for(ids)
        text = _text(rng, ids)
        seeded = {rng.randrange(len(probes.ids))} if rng.random() < 0.1 and probes.ids else set()
        new, old = set(seeded), set(seeded)
        _add_text_hits(text, probes, new)
        linear_hits(text, probes, old)
        assert new == old, (ids, text)
        checked += bool(old)
    assert checked > 1000  # a fuzz that found nothing would agree with anything


def test_a_long_run_of_hex_digits_is_searched_as_a_whole() -> None:
    wanted, other = str(uuid4()), str(uuid4())
    probes = _probes_for([wanted, other, *(str(uuid4()) for _ in range(_LINEAR_PROBES))])
    filler = "ab" * (_LONG_RUN // 2 + 10)
    for text in (filler + wanted.replace("-", "") + filler, wanted.replace("-", "") + filler, filler + wanted.replace("-", "")):
        hits = set()
        _add_text_hits(text, probes, hits)
        assert hits == {0}, len(text)
    hits = set()
    _add_text_hits(filler, probes, hits)
    assert hits == set()


def test_every_window_of_a_run_is_read() -> None:
    wanted = str(uuid4())
    probes = _probes_for([wanted, *(str(uuid4()) for _ in range(_LINEAR_PROBES))])
    digits = wanted.replace("-", "")
    for before in range(0, 40):
        for after in (0, 1, 7, 33):
            hits = set()
            _add_text_hits(("c" * before + digits + "e" * after).lower(), probes, hits)
            assert hits == {0}, (before, after)


def test_ids_with_leading_zeros_and_with_short_digits_are_found() -> None:
    zero_lead = "000" + uuid4().hex[3:]
    short = "00000000-0000-0000-0000-00000000" + "abcd"
    probes = _probes_for([zero_lead, short])
    assert _citation_probe(zero_lead)[1] == zero_lead.lstrip("0")
    for item, expected in ((zero_lead, {0}), (short, {1})):
        hits = set()
        _add_text_hits(f"see {item} here", probes, hits)
        assert expected <= hits
    hits = set()
    _add_text_hits("nothing of the kind", probes, hits)
    assert hits == set()


def test_the_cost_does_not_grow_with_the_number_of_ids() -> None:
    texts = [(" ".join(str(uuid4()) for _ in range(3)) + " connection explanation words " * 4).lower() for _ in range(300)]
    few = _probes_for([str(uuid4()) for _ in range(_LINEAR_PROBES + 1)])
    many = _probes_for([str(uuid4()) for _ in range(5000)])

    def run(probes):
        best = None
        for _ in range(3):
            started = time.perf_counter()
            for text in texts:
                _add_text_hits(text, probes, set())
            elapsed = time.perf_counter() - started
            best = elapsed if best is None else min(best, elapsed)
        return best

    # Looking for 5,000 ids one by one is a thousand times the work of looking for 5; the table makes it about the same.
    assert run(many) < 8 * run(few) + 0.02, (run(many), run(few))
