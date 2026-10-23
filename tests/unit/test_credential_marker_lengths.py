"""A short code after a credential marker is not a credential (2026-10-22).

The defect. ``legacy_credential_check`` read each token marker (``ghp_``, ``npm_``,
``sk-`` and the others) as a credential wherever it started a word, with nothing
required after it. So ``SK-2291 is the current seal kit``, ``sk-2291 is current``,
``SK-229`` and ``npm_config_cache`` were refused at the commit gate, and that gate
is what ``alice_capture`` and the importers use. ``alice-memory import-markdown``
replaced every such line with the withheld marker and dropped a whole file whose
name held such a code. ``alice_capture`` refused the whole capture. A parts list,
an order history or a ticket log lost lines or could not be captured.

The fix. Each marker counts only when at least a floor of token characters follows
it, and the floor is no longer than the shortest body a real token of that kind
has. ``legacy._TOKEN_BODIES`` states the shape behind each number and where it
comes from. The table below repeats the numbers independently, so a change to a
floor has to change a test too.

The ``ghs_`` marker reads the class GitHub publishes for installation tokens (letters,
digits, dot, hyphen, underscore), because GitHub's stateless form is ``ghs_``, a
numeric app id, an underscore and a JWT of about 520 characters. With base62 alone,
an app id of 1 to 7 digits ended the run before 36 characters and the token was not
read by the marker rule at all. ``ghu_`` takes the same class and floor by our
choice, because GitHub announced a ``ghu_`` format change without details.

A token right after an underscore is read too. v0.16.0's lead boundary was "no
letter, digit or underscore before the marker", so ``_ghs_...``, ``x_ghs_...`` and
``TOKEN_npm_...`` were never read by the marker rule. Every token marker now has a
floor, which is what keeps ordinary names out, so a marker with a floor is hidden
only by a letter or a digit before it (``Xghs_...`` stays unread). The ssh markers
have no floor and keep v0.16.0's lead.

What is pinned here:

* the short codes, and the ordinary words that start with a marker, pass the
  commit gate, the promotion floor, the credential floor and both doors. That
  includes ``SK-20260301``, ``SK-2025-0618`` and ``SK-2291-REV``, which the commit
  gate's own ``sk-`` prefix pattern (``sk-`` and 8 characters) read as keys until it
  took the floor of the ``sk-`` marker;
* for every marker, a real-shaped synthetic token is still read at line start,
  inside a sentence, after ``=`` and ``:``, in a JSON string, in upper case, in a
  file name, on a second line, spaced out, and at exactly the shortest real length;
* the line is drawn at the floor: one character more is read, one fewer is not by
  the marker rule or by the ``sk-`` prefix pattern (the commit gate's other rules
  and the credential floor may still read it, and the tests that say "passes" mean
  those two rules alone);
* a real-shaped ``sk-`` key, at exactly 32 characters after the prefix and in the
  longer forms, is read by the prefix pattern in every placement;
* a stateless GitHub app token (``ghs_`` and ``ghu_``, an app id of 1, 4, 7 and 10
  digits, an unsigned JWT shaped body built here from sequential runs), the same
  after an underscore, and a token of every other family after an underscore
  (``x_ghs_``, ``TOKEN_npm_``, ``_sk-``, ``_glpat-``, ``_github_pat_``, ``_xoxb-``)
  are read in every placement, and refused and not stored at every door: a direct memory commit,
  ``alice_capture``, ``alice-memory import-markdown``, the ChatGPT import (with
  ordinary text after the token in the same message, and with the token alone) and
  the promotion credential floor, with the classic 40 character token beside it;
  short names such as ``GHS_2291``, ``ghs_12_ab``, ``ghu_config``, ``PART_SK-2291`` and
  ``deploy_ghu_config`` still pass, with or without an underscore before them, and a
  ``ghs_`` followed by 36 or more identifier characters is read, which is the accepted
  cost of GitHub's own pattern;
* the two ssh markers keep the bare reading, the ``sk-learn`` word chain and the
  FIDO key types are excused as before, and a token glued onto a FIDO key type is
  still refused;
* ``alice-memory import-markdown`` keeps a note full of such codes whole and still
  withholds a real-shaped key, and ``alice_capture`` accepts such text and still
  refuses one (``tests/integration/test_credential_marker_lengths_postgres.py`` runs
  the capture on PostgreSQL);
* the patterns stay linear.

Mutations, each alone (``scripts/derived_label_mutations.json`` replays them):
the ``sk-`` floor set to 0, the bare marker (``test_a_short_code_is_not_a_credential``);
the ``sk-`` floor set one above the shortest real body, 33
(``test_a_real_shaped_token_is_still_caught``); the same two for ``npm_``;
the ``sk-learn`` carve-out removed
(``test_a_word_chain_long_enough_to_be_read_is_excused_by_its_carve_out``); the FIDO
alternative removed from the ``sk-`` pattern
(``test_a_token_glued_onto_a_fido_key_type_is_still_refused_on_both_paths``); the
floor dropped from the unbounded patterns, which read a spaced-out run
(``test_a_spaced_out_short_code_is_not_a_credential``); the ``sk-`` prefix pattern
back to 8 characters (``test_the_sk_prefix_rule_has_the_markers_floor``); the
``ghs_`` class back to base62 alone, and the same for ``ghu_``
(``test_a_real_shaped_token_is_still_caught``); the ``ghs_`` floor set to 37
(the same test, on the classic 40 character token); the lead boundary back to
v0.16.0's, which counts an underscore (the same test, on the underscore placements).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import sqlite3
import string
from uuid import uuid4

import pytest

from alicebot_api import legacy_credential_check as legacy
from alicebot_api.credential_floor import carries_credential_material, credential_verdict
from alicebot_api.legacy_credential_check import commit_door_fields_verdict
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool
from alicebot_api.onramp import bootstrap_database, main as onramp_main, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload
from alicebot_api.vnext_promotion_policy import PromotionCandidate, hard_floor_hits

from tests.unit import legacy_v0160_credential_oracle as oracle
from tests.unit.fixtures_timing import SIZE_RATIO, assert_grows_linearly, budget, smallest_cpu_seconds, tracer_active

USER_ID = "00000000-0000-4000-8000-0000000000bb"
CLEAN = "The indigo lighthouse stays on the hill for the weekly import."
WITHHELD = "[withheld: credential material]"

# ---------------------------------------------------------------------------
# Synthetic tokens. Every body is a repeated or sequential run, built at run time
# from pieces, so no whole scanner-shaped token sits in the source and none is a
# real key.
# ---------------------------------------------------------------------------

# Upper case, lower case and a digit in turn: "Aa0Bb1Cc2...". Mixed case, as a
# base62 body is, and obviously not random.
_MIXED = "".join(
    f"{upper}{lower}{digit}"
    for upper, lower, digit in zip(string.ascii_uppercase, string.ascii_lowercase, itertools.cycle(string.digits))
)
_DIGITS = string.digits
_HEX = "0123456789abcdef"


def _seq(size: int, alphabet: str = _MIXED) -> str:
    return (alphabet * (size // len(alphabet) + 1))[:size]


def _urlsafe(size: int) -> str:
    """A body of the base64url alphabet: the sequential run with a hyphen and an underscore in it."""

    body = list(_seq(size))
    for index, char in ((12, "-"), (25, "_"), (40, "-")):
        if index < size:
            body[index] = char
    return "".join(body)


def _groups(*sizes: int) -> str:
    return "-".join(_seq(size, _DIGITS) for size in sizes)


def _jwt_body() -> str:
    """Three base64url parts and two dots, 510 characters, unsigned: sequential runs, never a real token."""

    return ".".join(("eyJ" + _urlsafe(33), "eyJ" + _urlsafe(127), _urlsafe(342)))


def _app_token(marker: str, digits: int) -> str:
    """GitHub's stateless form: the prefix, a numeric app id, an underscore and a JWT shaped body."""

    return marker + _seq(digits, "1234567890") + "_" + _jwt_body()


_APP_ID_DIGITS = (1, 4, 7, 10)


# marker -> (shortest body of a real token, floor). The floor is never longer than
# the shortest real body. The published shape of each is in legacy._TOKEN_BODIES.
_SHAPE_TABLE: dict[str, tuple[int, int]] = {
    "ghp_": (36, 36),
    "gho_": (36, 36),
    "ghs_": (36, 36),
    "ghu_": (36, 36),
    "github_pat_": (82, 82),
    "glpat-": (20, 20),
    "npm_": (36, 36),
    "sk-": (32, 20),
    "xoxb-": (46, 10),
    "xoxp-": (65, 10),
    "xoxa-": (65, 10),
    "xoxs-": (65, 10),
}
_BARE_MARKERS = ("ssh-rsa ", "ssh-ed25519 ")


def _shortest_real(marker: str) -> str:
    """A synthetic token of this marker's shortest real shape."""

    if marker in ("ghp_", "gho_", "ghs_", "ghu_", "npm_"):
        return marker + _seq(36)
    if marker == "github_pat_":
        return marker + _seq(22) + "_" + _seq(59)
    if marker == "glpat-":
        return marker + _seq(20)
    if marker == "sk-":
        return marker + _seq(32, _HEX)
    if marker == "xoxb-":
        return marker + _groups(10, 10) + "-" + _seq(24)
    return marker + _groups(10, 10, 10) + "-" + _seq(32, _HEX)


def _real_shapes() -> dict[str, list[tuple[str, str]]]:
    """marker -> [(label, token)]: the shortest real shape and the other published shapes."""

    shapes: dict[str, list[tuple[str, str]]] = {}
    for marker in _SHAPE_TABLE:
        shapes[marker] = [("shortest", _shortest_real(marker))]
    for marker in ("ghs_", "ghu_"):
        for digits in _APP_ID_DIGITS:
            shapes[marker].append((f"stateless {digits} digit app id", _app_token(marker, digits)))
    shapes["github_pat_"].append(("93 characters", "github" + "_pat_" + _seq(22) + "_" + _seq(59)))
    shapes["glpat-"].append(("mixed", "glp" + "at-" + _urlsafe(26)))
    shapes["sk-"] += [
        ("classic 48", "sk" + "-" + _seq(48)),
        ("project", "sk" + "-proj-" + _urlsafe(120)),
        ("service account", "sk" + "-svcacct-" + _urlsafe(120)),
        ("admin", "sk" + "-admin-" + _urlsafe(100)),
        ("anthropic", "sk" + "-ant-api03-" + _urlsafe(93)),
        ("exactly the floor", "sk" + "-" + _urlsafe(20)),
    ]
    shapes["xoxb-"].append(("13 digit groups", "xo" + "xb-" + _groups(13, 13) + "-" + _seq(24)))
    shapes["xoxp-"].append(("13 digit groups", "xo" + "xp-" + _groups(13, 13, 13) + "-" + _seq(32, _HEX)))
    shapes["xoxa-"].append(("two groups", "xo" + "xa-2-" + _groups(12, 12, 12) + "-" + _seq(32, _HEX)))
    return shapes


def marker_hits(text: str, *, bounded: bool = True) -> list[str]:
    """The markers the marker rule alone reads in ``text``, ignoring every other rule and every carve-out."""

    return [
        str(finding.value)
        for finding in legacy._credential_findings(text, require_token_boundary=bounded)
        if finding.rule == "marker"
    ]


def _candidate(text: str) -> PromotionCandidate:
    return PromotionCandidate(
        title="parts note",
        canonical_text=text,
        domain="professional",
        sensitivity="internal",
        source_type="trusted_agent",
    )


# ---------------------------------------------------------------------------
# The table is the published shape, and the module agrees with it.
# ---------------------------------------------------------------------------


def test_the_module_floors_are_the_table_and_the_table_covers_every_marker() -> None:
    assert set(_SHAPE_TABLE) | set(_BARE_MARKERS) == set(legacy._CREDENTIAL_TOKEN_MARKERS)
    assert set(legacy._TOKEN_BODIES) == set(_SHAPE_TABLE)
    for marker, (shortest, floor) in _SHAPE_TABLE.items():
        assert legacy._TOKEN_BODIES[marker].floor == floor, marker
        # A floor never exceeds the shortest real body: every real token is read.
        assert floor <= shortest, marker
        # The synthetic shortest token has exactly that body.
        assert len(_shortest_real(marker)) - len(marker) == shortest, marker


# ---------------------------------------------------------------------------
# False positives: these are not credentials, anywhere.
# ---------------------------------------------------------------------------

_SHORT_CODES = (
    "SK-2291",
    "SK-22910",
    "SK-229",
    "sk-2291 is current",
    "SK-2291 is the current seal kit",
    "order SK-2192 from stores",
    "SK-2291, HD-40213, PO-55102, TB-12, NCR-0042",
    "2025-06-18-parts-note-legacy-seal-kit-sk-2192.md",
    "Parts: SK-2291 and sk-22910 and Sk-229 are the same kit.",
    # Longer codes, 8 or more characters after the dash. The commit gate's own prefix pattern read these as keys.
    "SK-20260301",
    "SK-2025-0618",
    "SK-2291-REV",
    "Order history: SK-20260301, SK-2025-0618 and SK-2291-REV were backordered.",
    "sk-2025-0618 and sk-2291-rev are the same two kits in lower case",
    "2026-03-01-order-history-SK-20260301.md",
    # Another marker with a short code after it.
    "GHP_2291 is a room code",
    "GHO_12 and GHS_9 and GHU_77",
    "GHS_2291",
    "ghs_12_ab",
    "GLPAT-2291 is the glue",
    "NPM_2291 is a bin",
    "XOXB-2291 and XOXP-77 and XOXA-5 and XOXS-1 are stamps",
    "github_pat_2291 is a folder",
)
_ORDINARY_WORDS = (
    "npm_config_cache",
    "ghu_config",
    # An underscore before the marker no longer hides it, so the floor alone keeps these out.
    "PART_SK-2291",
    "x_ghs_12_ab",
    "my_npm_config_cache",
    "deploy_ghu_config",
    "ghs_app_token_cache_dir",
    "the ghu_release_notes file is old",
    "set npm_config_registry to the mirror",
    "npm_package_version is set by npm",
    "npm_lifecycle_event",
    "Fine-grained tokens start with github_pat_ and classic ones with ghp_",
    "a token that starts with ghp_ is a GitHub token",
    "sk-learn",
    "We use sk-learn for the churn model.",
    "the sk-learn-bench-2024 run",
    "pip install sk-image",
    "SK-LEARN is the old name",
    "AB-2291 is current",
)
_FALSE_POSITIVES = _SHORT_CODES + _ORDINARY_WORDS


@pytest.mark.parametrize("text", _FALSE_POSITIVES)
def test_a_short_code_is_not_a_credential(text: str) -> None:
    assert marker_hits(text) == []
    assert marker_hits(text, bounded=False) == []
    # The commit gate (which capture and the importers use), the promotion floor and the credential floor.
    assert commit_door_fields_verdict(text) is None
    assert not legacy.commit_gate_refuses("", text)
    assert not legacy.promotion_floor_refuses("", text)
    assert credential_verdict(text) is None
    assert not carries_credential_material(text)
    assert "credential_material" not in hard_floor_hits(_candidate(text))
    # And as a title, where the commit door reads it too. The body is lower case and short: the floor also reads the
    # fields joined with nothing, so a title ending in a word glues to the first word of the body.
    assert legacy.commit_door_secret_verdict(text, "note") is None


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_a_marker_glued_to_a_letter_or_a_digit_is_still_not_read(marker: str) -> None:
    """The boundary that stays: a letter or a digit before a marker makes it part of a word (and dropping that would
    read inside urlsafe base64 blobs). An underscore before it no longer does. Marker rule alone."""

    token = _shortest_real(marker)
    for lead in ("X", "x", "7", "Q9"):
        assert marker not in marker_hits(lead + token), lead
        assert marker not in marker_hits(f"deploy {lead}{token}"), lead
    for lead in ("_", "x_", "TOKEN_", "9_"):
        assert marker in marker_hits(lead + token), lead


@pytest.mark.parametrize("text", _SHORT_CODES + ("sk-learn", "npm_config_cache"))
def test_v0160_refused_these_which_is_the_defect(text: str) -> None:
    """Guards the guard: v0.16.0's own code read the bare marker and refused every one, so a pass above is the fix."""

    assert oracle.commit_gate_refuses("", text) or oracle.promotion_floor_refuses("", text)


def test_the_false_positive_list_holds_a_short_code_for_every_floored_marker() -> None:
    """Guards the guard: a marker added to the table without a short-code row fails here."""

    joined = " ".join(_SHORT_CODES).casefold()
    for marker in _SHAPE_TABLE:
        assert marker.casefold() in joined, marker


def _memory_store() -> tuple[sqlite3.Connection, SQLiteVNextStore, str]:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "marker-lengths@example.com")
    return conn, SQLiteVNextStore(conn, user_id), user_id


def test_a_memory_commit_keeps_a_note_with_short_codes() -> None:
    conn, store, user_id = _memory_store()
    payload = {
        "title": "Seal kit SK-2291",
        "canonical_text": "SK-2291 is the current seal kit; order SK-2192 from stores.",
    }
    result = VNextMemoryCommitService(store).commit(
        identity=None, request=memory_commit_request_from_payload(payload, user_id=user_id)
    )
    assert result["status"] != "rejected", result.get("reasons")
    assert int(next(iter(conn.execute("SELECT count(*) FROM memories")))[0]) == 1


# ---------------------------------------------------------------------------
# True positives: a real-shaped token is read in every placement.
# ---------------------------------------------------------------------------

_PLACEMENTS = {
    "line start": lambda token: token,
    "in a sentence": lambda token: f"Rotate the deploy secret {token} before Friday.",
    "after equals": lambda token: f"DEPLOY_SECRET={token}",
    "after colon": lambda token: f"deploy secret: {token}",
    "in a json string": lambda token: json.dumps({"deploy": token}),
    "second line": lambda token: f"Notes for Friday.\n{token}\nDone.",
    "upper case spelling": lambda token: token.upper(),
    "in a file name": lambda token: f"2025-06-18-deploy-notes-{token}.md",
    # v0.16.0's lead boundary hid a token right after an underscore. A marker with a floor is hidden only by a letter
    # or a digit before it now.
    "right after an underscore": lambda token: f"_{token}",
    "after a word and an underscore": lambda token: f"x_{token}",
    "after a name in capitals and an underscore": lambda token: f"TOKEN_{token}",
}
_CASES = [
    pytest.param(marker, token, placement, id=f"{marker}-{label}-{placement}")
    for marker, shapes in _real_shapes().items()
    for label, token in shapes
    for placement in _PLACEMENTS
]


@pytest.mark.parametrize(("marker", "token", "placement"), _CASES)
def test_a_real_shaped_token_is_still_caught(marker: str, token: str, placement: str) -> None:
    text = _PLACEMENTS[placement](token)
    # The marker rule alone reads it, so a change to the floor shows here and not only where another rule also reads.
    assert marker in marker_hits(text), text
    assert legacy.commit_gate_refuses("", text)
    assert legacy.promotion_floor_refuses("", text)
    assert commit_door_fields_verdict(text) == "credential"


def prefix_hits(text: str) -> list[str]:
    """The ``sk-`` prefix findings of the commit gate's own rule on one field, with no other rule and no carve-out."""

    return [finding.rule for finding in legacy._commit_field_findings(text) if finding.rule == "prefix_sk"]


_SK_CASES = [
    pytest.param(token, placement, id=f"{label}-{placement}")
    for label, token in _real_shapes()["sk-"]
    for placement in _PLACEMENTS
]


@pytest.mark.parametrize(("token", "placement"), _SK_CASES)
def test_a_real_shaped_sk_key_is_still_read_by_the_prefix_rule_in_every_placement(token: str, placement: str) -> None:
    """The commit gate's own ``sk-`` pattern reads the shortest real key (32 characters after the prefix), the
    classic 48, the project, service account, admin and Anthropic forms, and a body of exactly the floor."""

    text = _PLACEMENTS[placement](token)
    assert prefix_hits(text) == ["prefix_sk"], text
    assert legacy.commit_gate_refuses("", text)
    assert commit_door_fields_verdict(text) == "credential"


def test_a_lower_case_sk_code_of_8_digits_is_read_by_the_credential_floor_alone() -> None:
    """A residual, pinned so a change is seen. The credential floor (``credential_floor``, not this module) reads
    lower-case ``sk-`` followed by 8 or more characters that hold a digit, unless they are a chain of short words and
    numbers, so ``sk-20260301`` is still refused at every door. Upper case (``SK-20260301``) is a part number to it,
    and ``sk-2025-0618`` and ``sk-2291-rev`` are word chains. Neither rule in this module reads any of them."""

    for code in ("sk-20260301", "order sk-20260301 today"):
        assert marker_hits(code) == [] and prefix_hits(code) == [], code
        assert not legacy.commit_gate_refuses("", code), code
        assert credential_verdict(code) is not None, code
    for code in ("SK-20260301", "sk-2025-0618", "sk-2291-rev"):
        assert credential_verdict(code) is None, code


def test_the_sk_prefix_rule_has_the_markers_floor() -> None:
    """The prefix rule reads ``sk-`` and exactly as many token characters as the marker does, and no fewer.

    A body one short of the floor is allowed to pass this rule by design; the credential floor may still read it.
    """

    floor = legacy._TOKEN_BODIES["sk-"].floor
    assert floor == _SHAPE_TABLE["sk-"][1] == 20
    assert legacy._PREFIX_SK.pattern.endswith("{%d,}" % floor)
    for placement in _PLACEMENTS.values():
        assert prefix_hits(placement(_with_body("sk-", floor))) == ["prefix_sk"]
        assert prefix_hits(placement(_with_body("sk-", floor + 12))) == ["prefix_sk"]
        assert prefix_hits(placement(_with_body("sk-", floor - 1))) == []
        assert prefix_hits(placement(_with_body("sk-", 8))) == []
    # The three longer codes are 8 to 9 characters after the dash, in either case.
    for code in ("SK-20260301", "SK-2025-0618", "SK-2291-REV", "sk-2025-0618"):
        assert prefix_hits(code) == [], code
        assert prefix_hits(f"order {code} today") == [], code
        assert oracle.commit_gate_refuses("", code), code  # v0.16.0 read each of them as a key


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_every_marker_is_read_when_it_is_the_whole_shortest_real_token_in_a_title_or_a_ref(marker: str) -> None:
    token = _shortest_real(marker)
    assert legacy.commit_gate_refuses(token, "Parts note")
    assert legacy.commit_gate_refuses("Parts note", "see refs", source_refs=[token])
    assert legacy.promotion_floor_refuses(token, "Parts note")


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_a_spaced_out_real_token_is_still_caught(marker: str) -> None:
    """A run of single characters is collapsed and read without the lead boundary, as v0.16.0 read it."""

    token = _shortest_real(marker)
    spaced = " ".join(token)
    assert marker in marker_hits(token, bounded=False)
    assert legacy.commit_gate_refuses("", spaced)
    assert legacy.promotion_floor_refuses("", spaced)


@pytest.mark.parametrize("text", ("S K - 2 2 9 1 0", "S K - 2 2 9 1 0 4", "N P M _ c o n f i g", "G H P _ 2 2 9 1 0 7"))
def test_a_spaced_out_short_code_is_not_a_credential(text: str) -> None:
    assert marker_hits("".join(text.split()), bounded=False) == []
    assert commit_door_fields_verdict(text) is None
    assert not legacy.promotion_floor_refuses("", text)


# ---------------------------------------------------------------------------
# The GitHub app markers read GitHub's own class.
# ---------------------------------------------------------------------------


def test_ghs_and_ghu_read_the_class_github_recommends_and_ghp_and_gho_stay_base62() -> None:
    """GitHub's changelog of 2026-05-15 on installation tokens publishes ``ghs_[A-Za-z0-9\\.\\-_]{36,}`` for both
    formats of ``ghs_`` and announces that ``ghu_`` will change format too, without details, so ``ghu_`` takes the same
    class and floor by our choice. Nothing published says so for ``ghp_`` and ``gho_``."""

    assert legacy._TOKEN_BODIES["ghs_"].chars == legacy._TOKEN_BODIES["ghu_"].chars == "0-9A-Za-z._-"
    assert legacy._TOKEN_BODIES["ghp_"].chars == legacy._TOKEN_BODIES["gho_"].chars == "0-9A-Za-z"
    for separator in (".", "-", "_"):
        body = ("ab" + separator) * 12
        assert len(body) == 36
        for marker in ("ghs_", "ghu_"):
            assert marker in marker_hits(marker + body), (marker, separator)
        for marker in ("ghp_", "gho_"):
            assert marker not in marker_hits(marker + body), (marker, separator)


@pytest.mark.parametrize("marker", ("ghs_", "ghu_"))
def test_a_ghs_or_ghu_followed_by_36_identifier_characters_is_read_as_github_reads_it(marker: str) -> None:
    """The accepted cost of GitHub's pattern: an identifier of 36 or more letters, digits, dots, hyphens and
    underscores right after the marker is read, whatever it means. A shorter one is not."""

    long_name = "app_installation_token_cache_directory"
    short_name = "app_installation_token_cache_dir"
    assert len(long_name) >= 36 > len(short_name)
    assert marker in marker_hits(f"set {marker}{long_name} first")
    assert legacy.commit_gate_refuses("", f"set {marker}{long_name} first")
    assert legacy.promotion_floor_refuses("", f"set {marker}{long_name} first")
    for text in (f"set {marker}{short_name} first", f"{marker}app_token_cache_dir", f"{marker}config"):
        assert marker_hits(text) == [], text
        assert commit_door_fields_verdict(text) is None, text
    # And exactly at the floor, on the dotted and hyphenated spellings of an identifier.
    assert marker in marker_hits(marker + ("a.b-c_d" * 6)[:36])
    assert marker not in marker_hits(marker + ("a.b-c_d" * 6)[:35])


# ---------------------------------------------------------------------------
# Where the line is drawn.
# ---------------------------------------------------------------------------


def _with_body(marker: str, size: int) -> str:
    if marker == "github_pat_":
        return marker + _seq(size)
    if marker in ("xoxb-", "xoxp-", "xoxa-", "xoxs-"):
        return marker + _seq(size, _DIGITS + "abcdef")
    if marker in ("glpat-", "sk-"):
        return marker + _urlsafe(size)
    return marker + _seq(size)


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_a_body_of_exactly_the_floor_is_read(marker: str) -> None:
    floor = _SHAPE_TABLE[marker][1]
    assert marker in marker_hits(_with_body(marker, floor))
    assert marker in marker_hits(_with_body(marker, floor + 1))
    assert marker in marker_hits(f"use {_with_body(marker, floor)} now")


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_a_body_one_below_the_floor_passes_the_marker_rule_by_design(marker: str) -> None:
    """The marker rule draws its line at the floor. A body one short of it is allowed to pass this rule.

    Other rules may still read it: the commit gate's prefix patterns read ``ghp_`` and ``xoxb-`` from 8 characters, and
    the credential floor reads most shapes from 8 or 16. This test is about the marker rule alone.
    """

    floor = _SHAPE_TABLE[marker][1]
    for size in (floor - 1, max(floor - 1, 1) // 2, 0):
        text = _with_body(marker, size)
        assert marker not in marker_hits(text), (marker, size)
        assert marker not in marker_hits(f"use {text} now"), (marker, size)


@pytest.mark.parametrize("marker", sorted(_SHAPE_TABLE))
def test_a_body_that_leaves_the_token_alphabet_is_cut_there(marker: str) -> None:
    """The floor counts token characters, so a space or a dot inside the body ends the count."""

    floor = _SHAPE_TABLE[marker][1]
    body = _with_body(marker, floor)
    cut = marker + body[len(marker) : len(marker) + floor // 2] + " " + body[len(marker) + floor // 2 :]
    assert marker not in marker_hits(cut)


# ---------------------------------------------------------------------------
# The ssh markers keep the bare reading.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", _BARE_MARKERS)
def test_the_ssh_markers_keep_the_bare_reading_and_the_public_key_carve_out(marker: str) -> None:
    """Decided 2026-10-22: no floor for these two. They name a public key line, which carve-out a excuses, and a
    space after a key type name is not how a product code is written. Each text below is read by the marker rule as
    v0.16.0 read it, and who is excused is unchanged."""

    excused = {
        marker + "AAAA" + _seq(40): "a public key",
        f"Prefer {marker}keys over the rest": "a key type name and a word",
    }
    refused = {
        f"{marker}2048": "digits after a key type name",
        marker + _seq(40): "a body that is not a public key",
    }
    for text in (*excused, *refused):
        assert marker in marker_hits(text), text
        assert oracle.commit_gate_refuses("", text), text
    for text in excused:
        assert not legacy.commit_gate_refuses("", text), text
    for text in refused:
        assert legacy.commit_gate_refuses("", text), text
    # A body that is not a public key is read in every placement, as the other markers are.
    for placement, place in _PLACEMENTS.items():
        if "underscore" in placement:
            continue
        placed = place(marker + _seq(40))
        assert marker in marker_hits(placed), (placement, placed)
        assert legacy.commit_gate_refuses("", placed), (placement, placed)
        assert legacy.promotion_floor_refuses("", placed), (placement, placed)
    # They keep v0.16.0's lead too (decided 2026-10-22): no floor, so an underscore before them still hides them. A
    # public key line is not a secret, and the marker is not how a private key is found.
    for lead in ("_", "x_", "TOKEN_"):
        assert marker not in marker_hits(lead + marker + _seq(40)), lead
        assert not legacy.commit_gate_refuses("", lead + marker + _seq(40)), lead
    index = legacy._CREDENTIAL_TOKEN_MARKERS.index(marker)
    assert legacy._CREDENTIAL_TOKEN_PATTERNS[index].pattern == oracle._CREDENTIAL_TOKEN_PATTERNS[index].pattern


# ---------------------------------------------------------------------------
# The carve-outs, as before.
# ---------------------------------------------------------------------------

_FIDO = "sk-ssh-ed25519@openssh.com"
_FIDO_ECDSA = "sk-ecdsa-sha2-nistp256@openssh.com"


def test_a_word_chain_long_enough_to_be_read_is_excused_by_its_carve_out() -> None:
    """The marker rule reads ``sk-`` plus 20 characters, so a long lower-case chain is a finding, and the carve-out
    excuses it. The promotion floor has no prefix pattern, so only the carve-out stands between this text and a
    refusal."""

    for text in (
        "we ran sk-learn-benchmark-results-2024 again",
        "see sk-learn-bench-2024-and for details",
        "run_sk-learn-benchmark-results-2024 again",
    ):
        assert "sk-" in marker_hits(text), text
        assert not legacy.promotion_floor_refuses("", text), text
        assert not legacy.commit_gate_refuses("", text), text
    # Upper case is not excused, and more than three words after the first make no chain.
    for text in ("we ran SK-LEARN-BENCHMARK-RESULTS-2024 again", "see sk-learn-bench-2024-and-more-notes here"):
        assert "sk-" in marker_hits(text), text
        assert legacy.promotion_floor_refuses("", text), text


def test_the_fido_key_types_are_excused_alone_and_read_at_any_length() -> None:
    for text in (
        _FIDO,
        _FIDO_ECDSA,
        f"FIDO keys show as {_FIDO} in authorized_keys",
        f"_{_FIDO}",
        f"deploy_key_{_FIDO_ECDSA}",
    ):
        # Short bodies, read because a carve-out owns them, and excused by it.
        assert "sk-" in marker_hits(text), text
        assert not legacy.commit_gate_refuses("", text), text
        assert not legacy.promotion_floor_refuses("", text), text


@pytest.mark.parametrize("lead", ("", "_", "deploy_key_"))
@pytest.mark.parametrize("name", (_FIDO, _FIDO_ECDSA))
@pytest.mark.parametrize("marker", ("ghp_", "glpat-", "xoxb-", "npm_", "sk-"))
def test_a_token_glued_onto_a_fido_key_type_is_still_refused_on_both_paths(
    name: str, marker: str, lead: str
) -> None:
    glued = lead + name + _shortest_real(marker)
    # The commit gate has the prefix pattern as well; the promotion floor has only the marker rule, which reads the
    # FIDO name whatever its length and lets the carve-out's boundary rule refuse the glued token.
    assert legacy.promotion_floor_refuses("", glued), glued
    assert legacy.commit_gate_refuses("", glued), glued
    assert not legacy.promotion_floor_refuses("", lead + name)
    assert hard_floor_hits(_candidate(glued)), glued


# ---------------------------------------------------------------------------
# Linear time. The patterns look at most the floor ahead, then stop.
# ---------------------------------------------------------------------------

_LINEAR_UNITS = {
    "sk- one short of the floor": "sk-" + "a" * 19 + "\n",
    "ghp_ one short of the floor": "ghp_" + "a" * 35 + "\n",
    "github_pat_ one short of the floor": "github_pat_" + "a" * 81 + "\n",
    "xoxb- one short of the floor": "xoxb-" + "1" * 9 + "\n",
    "ghs_ one short of the floor, with dots": "ghs_" + "a._-" * 8 + "abc" + "\n",
    "ghu_ one short of the floor, with dots": "ghu_" + "1_." * 11 + "ab" + "\n",
    "ghs_ chain": "ghs_",
    "ghs_ jwt shaped run": "ghs_1234_" + "eyJ" + "a.b-c_" * 20 + "\n",
    "sk- chain": "sk-",
    "github_pat_ chain": "github_pat_",
    "npm_ chain": "npm_",
    "markers inside a body": "sk-ghp_npm_xoxb-glpat-",
}
_BUDGET_SECONDS = 2.0


def _read_everything(text: str) -> int:
    """Every finding of the marker rule, bounded and unbounded, and of the commit gate's own field rules."""

    return (
        sum(1 for _ in legacy._credential_findings(text))
        + sum(1 for _ in legacy._credential_findings(text, require_token_boundary=False))
        + sum(1 for _ in legacy._commit_field_findings(text))
    )


@pytest.mark.parametrize("label", sorted(_LINEAR_UNITS))
def test_the_floored_patterns_are_linear(label: str) -> None:
    unit = _LINEAR_UNITS[label]
    small = (unit * (25_000 // len(unit) + 1))[:25_000]
    large = (unit * (25_000 * SIZE_RATIO // len(unit) + 1))[: 25_000 * SIZE_RATIO]
    small_seconds, large_seconds = smallest_cpu_seconds(
        lambda: _read_everything(small), lambda: _read_everything(large), give_up_over=budget(_BUDGET_SECONDS)
    )
    assert large_seconds < budget(_BUDGET_SECONDS), (label, large_seconds, tracer_active())
    assert_grows_linearly(small_seconds, large_seconds, what=label)


# ---------------------------------------------------------------------------
# The doors: alice-memory import-markdown and alice_capture on SQLite.
# ---------------------------------------------------------------------------

_NOTE_NAME = "2025-06-18-parts-note-legacy-seal-kit-sk-2192.md"
# File names that hold a code: the short one, and the three longer ones the commit gate's prefix pattern read as keys.
_NOTE_NAMES = (
    _NOTE_NAME,
    "2026-03-01-order-history-SK-20260301.md",
    "2025-06-18-seal-kit-sk-2025-0618.md",
    "2025-06-18-seal-kit-sk-2291-rev.md",
)
_NOTE_LINES = (
    "Fact: SK-2291 is the current seal kit.",
    "Fact: order SK-2192 from stores.",
    "Parts: SK-2291, HD-40213, PO-55102, TB-12, NCR-0042",
    "Note: sk-2291 is current and SK-229 is retired.",
    "Fact: SK-20260301 shipped to the east depot.",
    "Order history: SK-2025-0618 and SK-2291-REV were backordered.",
    "Fact: set npm_config_cache to the shared folder.",
    f"Fact: {CLEAN}",
)
_REAL_KEYS = {
    "ghp_": "gh" + "p_" + _seq(36),
    "npm_": "np" + "m_" + _seq(36),
    "sk-": "sk" + "-" + _seq(48),
    "sk-proj-": "sk" + "-proj-" + _urlsafe(120),
    "glpat-": "glp" + "at-" + _seq(20),
    "github_pat_": "github" + "_pat_" + _seq(22) + "_" + _seq(59),
    "xoxb-": "xo" + "xb-" + _groups(10, 10) + "-" + _seq(24),
}
# GitHub app tokens: the stateless form (app id of 1, 4, 7 and 10 digits, an underscore and a JWT shaped body) and the
# classic 40 character form, for both prefixes.
for _marker in ("ghs_", "ghu_"):
    for _digits in _APP_ID_DIGITS:
        _REAL_KEYS[f"{_marker}-stateless-{_digits}-digit-app-id"] = _app_token(_marker, _digits)
    _REAL_KEYS[f"{_marker}-classic-40"] = _marker + _seq(36)
# A token right after an underscore, for every family: the marker rule did not read these before 2026-10-22.
for _digits in _APP_ID_DIGITS:
    _REAL_KEYS[f"underscore-ghs_-stateless-{_digits}-digit-app-id"] = "_" + _app_token("ghs_", _digits)
    _REAL_KEYS[f"underscore-ghu_-stateless-{_digits}-digit-app-id"] = "_" + _app_token("ghu_", _digits)
_REAL_KEYS["x_ghs_-classic-36"] = "x_" + "gh" + "s_" + _seq(36)
_REAL_KEYS["underscore-ghu_-classic-36"] = "_" + "gh" + "u_" + _seq(36)
_REAL_KEYS["TOKEN_npm_-36"] = "TOKEN_" + "np" + "m_" + _seq(36)
_REAL_KEYS["underscore-sk--20"] = "_sk" + "-" + _urlsafe(20)
_REAL_KEYS["underscore-sk--48"] = "_sk" + "-" + _seq(48)
_REAL_KEYS["underscore-glpat--20"] = "_glp" + "at-" + _seq(20)
_REAL_KEYS["underscore-github_pat_-82"] = "_github" + "_pat_" + _seq(22) + "_" + _seq(59)
_REAL_KEYS["underscore-xoxb--body"] = "_xo" + "xb-" + _groups(10, 10) + "-" + _seq(24)
# Every family at the doors that take one token: a direct memory commit, the ChatGPT import, the promotion floor.
_DOOR_CASES = [pytest.param(token, id=family) for family, token in _REAL_KEYS.items()]


def _db(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _blob(database: Path) -> str:
    chunks: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        names = [
            str(row[0] if isinstance(row, tuple) else row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        ]
        for name in names:
            if name.startswith("sqlite_"):
                continue
            try:
                rows = conn.execute(f"SELECT * FROM {name}").fetchall()
            except Exception:  # noqa: BLE001 - a virtual table the connection cannot scan is not a store row
                continue
            chunks.append(json.dumps(rows, default=str))
    return "\n".join(chunks)


def _import(folder: Path, database: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert onramp_main(["import-markdown", "--from", str(folder), "--db", str(database), "--user-id", USER_ID]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("name", _NOTE_NAMES)
def test_import_markdown_keeps_a_note_with_short_codes_whole_and_keeps_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Before the fix every code line was stored as the withheld marker and the file, named for a code, was dropped.

    Fails if the bare marker is read again: a line is withheld, or the file is skipped.
    """

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / name).write_text("\n".join((*_NOTE_LINES, "")), encoding="utf-8")
    database = _db(tmp_path / "data")
    receipt = _import(folder, database, capsys)
    assert receipt["imported_count"] == 1, receipt
    assert receipt["skipped_count"] == 0
    assert receipt["failed_count"] == 0
    assert receipt["skipped_credentials"] == 0
    assert receipt["skipped_credential_items"] == []
    stored = _blob(database)
    assert WITHHELD not in stored
    assert name in stored
    for line in _NOTE_LINES:
        assert line in stored, line


@pytest.mark.parametrize("family", sorted(_REAL_KEYS))
def test_import_markdown_still_withholds_a_real_shaped_key_and_keeps_the_codes_around_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], family: str
) -> None:
    key = _REAL_KEYS[family]
    folder = tmp_path / "notes"
    folder.mkdir()
    note = "\n".join(
        ("Fact: SK-2291 is the current seal kit.", f"Decision: use deploy key {key}", "Fact: order SK-2192.", "")
    )
    (folder / _NOTE_NAME).write_text(note, encoding="utf-8")
    database = _db(tmp_path / "data")
    receipt = _import(folder, database, capsys)
    assert receipt["imported_count"] == 1, receipt
    assert receipt["skipped_credentials"] == 1, receipt
    assert receipt["skipped_credential_items"] == [f"file 1 ({_NOTE_NAME}) line 2"]
    assert key not in json.dumps(receipt)
    stored = _blob(database)
    assert key not in stored
    assert "Fact: SK-2291 is the current seal kit." in stored
    assert "Fact: order SK-2192." in stored
    assert WITHHELD in stored


def test_import_markdown_still_skips_a_file_whose_name_holds_a_real_shaped_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _REAL_KEYS["ghp_"]
    folder = tmp_path / "notes"
    folder.mkdir()
    named_for_the_key = folder / f"2025-06-18-parts-note-{key}.md"
    named_for_the_key.write_text("Fact: SK-2291 is the current seal kit.\n", encoding="utf-8")
    database = _db(tmp_path / "data")
    receipt = _import(folder, database, capsys)
    assert receipt["status"] == "skipped", receipt
    assert receipt["skipped_count"] == 1
    assert receipt["imported_count"] == 0
    assert "file 1 (name withheld)" in receipt["skipped_credential_items"]  # type: ignore[operator]
    assert key not in json.dumps(receipt)
    assert key not in _blob(database)


def _sqlite_context(tmp_path: Path) -> MCPRuntimeContext:
    database = Path(tmp_path) / "memory.db"
    if not database.exists():
        bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)  # type: ignore[arg-type]


def _counts(database: Path) -> tuple[int, int]:
    with sqlite_user_connection(database, USER_ID) as conn:
        values: list[int] = []
        for table in ("sources", "source_chunks"):
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            values.append(int(row[0] if isinstance(row, tuple) else row["c"]))
        return values[0], values[1]


def test_alice_capture_accepts_text_with_short_codes(tmp_path: Path) -> None:
    """Before the fix the whole capture was refused (``tool_request_failed`` on the wire) for one code."""

    context = _sqlite_context(tmp_path)
    database = Path(tmp_path) / "memory.db"
    text = "\n".join(_NOTE_LINES)
    result = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": text, "title": "Seal kit SK-2291", "domain": "project", "sensitivity": "internal"},
    )
    assert result["status"] == "imported", result
    assert _counts(database)[0] == 1
    stored = _blob(database)
    for line in _NOTE_LINES:
        assert line in stored, line
    assert WITHHELD not in stored


@pytest.mark.parametrize("family", sorted(_REAL_KEYS))
def test_alice_capture_still_refuses_a_real_shaped_key(tmp_path: Path, family: str) -> None:
    key = _REAL_KEYS[family]
    context = _sqlite_context(tmp_path)
    database = Path(tmp_path) / "memory.db"
    clean = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": "SK-2291 is the current seal kit.",
            "title": "Seal kit",
            "domain": "project",
            "sensitivity": "internal",
        },
    )
    assert clean["status"] == "imported"
    before = _counts(database)
    with pytest.raises(MCPToolError, match="credential material"):
        call_mcp_tool(
            context,
            name="alice_capture",
            arguments={
                "raw_text": f"SK-2291 is the current seal kit. Use deploy key {key}",
                "title": "Seal kit",
                "domain": "project",
                "sensitivity": "internal",
            },
        )
    assert _counts(database) == before
    assert key not in _blob(database)


# ---------------------------------------------------------------------------
# Every _REAL_KEYS family at the doors that take one token: the GitHub app tokens
# (the stateless form, ghs_, an app id, an underscore and a JWT, and the classic
# 40 character form) and the same tokens right after an underscore. alice_capture
# and import-markdown take every family above, these included.
# ---------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, table: str) -> str:
    return json.dumps([list(row) for row in conn.execute(f"SELECT * FROM {table}")], default=str)


@pytest.mark.parametrize("token", _DOOR_CASES)
def test_a_direct_memory_commit_refuses_a_real_shaped_token_and_stores_nothing(token: str) -> None:
    """At v0.21.0 the bare marker refused a stateless token. With a base62 body alone and an app id of 1 to 7 digits
    the commit gate read nothing, and the memory was stored verbatim."""

    conn, store, user_id = _memory_store()
    payload = {"title": "Deploy note", "canonical_text": f"Use app token {token} for the deploy."}
    result = VNextMemoryCommitService(store).commit(
        identity=None, request=memory_commit_request_from_payload(payload, user_id=user_id)
    )
    assert result["status"] == "rejected", result
    assert "unsafe_secret_storage" in result["reasons"]
    assert int(next(iter(conn.execute("SELECT count(*) FROM memories")))[0]) == 0
    for table in ("memories", "memory_revisions", "event_log"):
        assert token[-24:] not in _rows(conn, table), table


def _chatgpt_export(path: Path, messages: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "id": "weekly",
                        "title": "Weekly",
                        "messages": [
                            {"author": {"role": "user"}, "content": {"parts": [message]}} for message in messages
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("shape", ("ordinary text after the token", "the token alone at the end"))
@pytest.mark.parametrize("token", _DOOR_CASES)
def test_the_chatgpt_import_withholds_a_message_holding_a_real_shaped_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], token: str, shape: str
) -> None:
    """With ordinary text after the token in the same message, the cross-field check that catches a token at the end of
    a transcript does not run on it, so the per-message check is the only door. Fails if that message is stored."""

    message = f"{token} then restart the service and check the logs." if shape.startswith("ordinary") else token
    export = tmp_path / "chat.json"
    _chatgpt_export(export, [CLEAN, message])
    database = _db(tmp_path / "data")
    assert onramp_main(["import-chatgpt", "--from", str(export), "--db", str(database), "--user-id", USER_ID]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "ok", receipt
    assert receipt["imported_count"] == 1
    assert receipt["failed_count"] == 0
    assert receipt["skipped_credentials"] == 1
    assert receipt["skipped_credential_items"] == ["conversation weekly message 2"]
    stored = _blob(database)
    assert token[-24:] not in stored
    assert token[-24:] not in json.dumps(receipt)
    assert CLEAN in stored
    assert WITHHELD in stored


@pytest.mark.parametrize("token", _DOOR_CASES)
def test_the_promotion_credential_floor_holds_back_a_real_shaped_token(token: str) -> None:
    assert "credential_material" in hard_floor_hits(_candidate(f"Use app token {token} for the deploy."))
    assert "credential_material" in hard_floor_hits(_candidate(token))
    assert legacy.promotion_floor_refuses("Deploy note", f"Use app token {token} for the deploy.")
    # Guards the guard: the same note with a short name instead of the token is not held back.
    assert "credential_material" not in hard_floor_hits(_candidate("Use app token ghs_12_ab for the deploy."))
