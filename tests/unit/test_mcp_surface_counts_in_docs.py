"""The tool counts the living docs state are the counts the registry lists.

The default three, the eleven core tools, the legacy long tail and the totals under each flag combination are written
by hand into `docs/integrations/mcp.md` and into `CURRENT_STATE.md`. Nothing derived them from the
registry, so a new tool definition or a moved tool would leave every page agreeing with itself and wrong.

This guard lists the tools the way a server does, under each flag combination, and reads the figures out of the pages:
those two, and the long-tail figures of `docs/alpha/mcp-tools.md`. The default three and the eleven are also stated
again, as a sentence, on many other living pages, so it reads the sentences of the living docs that give the default
count, the full count, or the count of core tools the default leaves out. Each of those numbers must be the one the
registry gives. A number it cannot place is a failure too when it sits beside a tool noun. A number that follows
`ALICE_MCP_FULL_TOOLS` within three words (digits only after `all`) is read as the full count and checked as one; a
number further from the flag, or digits without `all`, is not read there.

The scan reads the shapes the docs use and their near variants. It is not a proof that no copy can pass: a count
written with no tool noun, no mention of the flag and none of the other cues ("gives you all twelve") is not read,
because a bare "all twelve" more often names the things a sentence just listed.

Mutations, each one alone: add a twelfth core tool definition to `_CORE_TOOL_DEFINITIONS`; change `62` to `63` in
`docs/integrations/mcp.md`; delete `alice_explain` from the list in `ARCHITECTURE.md` while the name stays elsewhere
on that page; delete the `alice_explain` bullet under "The full core surface" in `docs/alpha/mcp-tools.md` while the
name stays elsewhere on that page; change `76-total` to `75-total` in `CURRENT_STATE.md`; change `default three tools`
to `default four tools` in `docs/alpha/hermes-skill.md`; change `all eleven core tools` to `all twelve core tools` in
`docs/alpha/onboarding.md`; change `The other eight core tools` to `The other seven core tools` in
`docs/integrations/hermes.md`; write `The server lists twelve core tools.` into any living page; change `(65)` to
`(66)` in `docs/alpha/mcp-tools.md`; change `exposes all eleven` to `exposes all twelve` in
`docs/security/auth-authorization.md`; write `ALICE_MCP_FULL_TOOLS=1 offers all twelve.` or `The full surface has
twelve.` into any living page; add an entry to `_NOT_A_SURFACE_COUNT` that no sentence matches. Each fails one of the
tests below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import scripts.check_control_doc_truth as control_doc_truth
from alicebot_api.mcp.registry import (
    _CORE_TOOL_DEFINITIONS,
    _DEFAULT_CORE_TOOL_ORDER,
    _LEGACY_TOOL_DEFINITIONS,
    list_mcp_tools,
)

ROOT = Path(__file__).resolve().parents[2]

_FLAGS = ("ALICE_AGENT_API_KEY", "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES")
_NUMBER_WORDS = {3: "three", 11: "eleven"}


def _word(number: int) -> str:
    """The word the pages use for 3 and 11, else the digits, so a new count fails on the page text, not on a lookup."""

    return _NUMBER_WORDS.get(number, str(number))


# Pages that name every core tool the default three do not include.
_PAGES_THAT_NAME_THE_OTHER_CORE_TOOLS = (
    "README.md",
    "ARCHITECTURE.md",
    "docs/integrations/mcp.md",
    "docs/alpha/mcp-tools.md",
)


def _flat(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def _listed(monkeypatch: pytest.MonkeyPatch, **flags: str) -> int:
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in flags.items():
        monkeypatch.setenv(name, value)
    return len(list_mcp_tools())


def _counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    return {
        "default": _listed(monkeypatch),
        "full": _listed(monkeypatch, ALICE_MCP_FULL_TOOLS="1"),
        "legacy": _listed(monkeypatch, ALICE_MCP_LEGACY_TOOLS="1"),
        "full_legacy": _listed(monkeypatch, ALICE_MCP_FULL_TOOLS="1", ALICE_MCP_LEGACY_TOOLS="1"),
        "legacy_surfaces": _listed(monkeypatch, ALICE_MCP_LEGACY_TOOLS="1", ALICE_LEGACY_SURFACES="1"),
        "all_three": _listed(
            monkeypatch,
            ALICE_MCP_FULL_TOOLS="1",
            ALICE_MCP_LEGACY_TOOLS="1",
            ALICE_LEGACY_SURFACES="1",
        ),
    }


def test_the_counts_in_the_guide_are_the_counts_the_registry_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _counts(monkeypatch)
    core = len(_CORE_TOOL_DEFINITIONS)
    default = len(_DEFAULT_CORE_TOOL_ORDER)

    # The registry itself agrees with the arithmetic the pages rely on.
    assert counts["default"] == default
    assert counts["full"] == core
    legacy_listed = counts["legacy"] - default
    assert counts["full_legacy"] == core + legacy_listed
    assert counts["all_three"] == core + len(_LEGACY_TOOL_DEFINITIONS)

    guide = _flat("docs/integrations/mcp.md")
    assert f"append {legacy_listed} retained long-tail memory tools" in guide
    assert (
        f"{legacy_listed} retained legacy memory tools are listed alongside whatever core set is enabled "
        f"({counts['legacy']} with the default {_word(default)}, {counts['full_legacy']} with the full "
        f"{_word(core)})"
    ) in guide
    assert f"the counts are {counts['legacy_surfaces']} and {counts['all_three']}" in guide

    # The same figures in the tools page, where they sit in a code block and a sentence.
    tools_page = _flat("docs/alpha/mcp-tools.md")
    assert f"The retained long tail has {legacy_listed} memory tools." in tools_page
    assert (
        f"# default {_word(default)} plus the long tail ({counts['legacy']}), or {counts['legacy_surfaces']} with "
        f"ALICE_LEGACY_SURFACES=1"
    ) in tools_page
    assert (
        f"# {_word(core)} plus the long tail ({counts['full_legacy']}), or {counts['all_three']} with "
        f"ALICE_LEGACY_SURFACES=1"
    ) in tools_page


def test_the_state_file_states_the_core_legacy_and_total_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _counts(monkeypatch)
    core = len(_CORE_TOOL_DEFINITIONS)
    legacy = len(_LEGACY_TOOL_DEFINITIONS)

    assert core + legacy == counts["all_three"]
    assert f"{core}-core/{legacy}-legacy/{counts['all_three']}-total" in _flat("CURRENT_STATE.md")


def _the_part_that_lists_the_tools(relative: str, names: list[str]) -> tuple[str, str]:
    """The text that has to name every tool, and how a name must sit in it.

    A name that appears somewhere else on a page does not prove the list on that page is whole, so the check reads one
    place. `mcp-tools.md` lists one bullet per tool under its full core surface heading. Every other page lists the
    tools in a single paragraph, so the paragraph that names the most of them is the list.
    """

    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "docs/alpha/mcp-tools.md":
        start = text.index("\n## The full core surface\n")
        end = text.index("\n## ", start + 1)
        return text[start:end], "bullet"
    paragraphs = re.split(r"\n\s*\n", text)
    return max(paragraphs, key=lambda block: sum(name in block for name in names)), "name"


@pytest.mark.parametrize("relative", _PAGES_THAT_NAME_THE_OTHER_CORE_TOOLS)
def test_the_pages_that_list_the_core_tools_name_every_tool_the_default_three_leave_out(relative: str) -> None:
    others = sorted(
        str(tool["name"]) for tool in _CORE_TOOL_DEFINITIONS if str(tool["name"]) not in _DEFAULT_CORE_TOOL_ORDER
    )
    assert others, "every core tool is on the default surface, so this guard checks nothing"

    scope, shape = _the_part_that_lists_the_tools(relative, others)
    missing = [name for name in others if (f"- `{name}`" if shape == "bullet" else name) not in scope]
    assert missing == [], f"{relative} does not list {missing} where it lists the core tools"


# --- Every sentence that gives a count of the surface -------------------------------------------------------------

_COUNT_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty"
).split()
_NUMBER = "(?:" + "|".join(_COUNT_WORDS) + r"|\d{1,2})"
# A number written as a word, or as one or two digits, that is not part of a longer token such as a version, a flag
# value (`ALICE_MCP_FULL_TOOLS=1`) or a path.
_COUNT_TOKEN = re.compile(rf"(?<![\w`/=.-])(?P<number>{_NUMBER})(?![\w`/=]|\.\d)", re.IGNORECASE)
_ABOUT_THE_SURFACE = re.compile(
    r"\btools?\b|ALICE_MCP_FULL_TOOLS|\bhandshake\b|\bsurface\b|\bregistry\b", re.IGNORECASE
)

# What may follow a number that is a count of tools: the noun, a connective, or the end of the clause. "all three
# doors" and "the other seven verbs" count something else.
_THEN_A_TOOL_COUNT_ENDS = re.compile(
    r"^(?:-tool\b|\s+(?:(?:default|core|MCP|extra|other|full)\s+)*tools?\b"
    r"|\s+(?:plus|with|are|stay|cannot|of\s+them|or|and)\b|\s*[,.;:)]|\s*$)",
    re.IGNORECASE,
)
# A number written beside a tool noun with none of the cues below: the scan does not know what it counts.
_BESIDE_A_TOOL_NOUN = re.compile(
    r"^(?:-tool\b|\s+(?:(?:default|core|MCP|extra|other|full)\s+)*tools?\b)",
    re.IGNORECASE,
)
_OTHERS_BEFORE = re.compile(r"\b(?:other|remaining|additional)\s+$", re.IGNORECASE)
_OTHERS_AFTER = re.compile(r"^\s+(?:extra|additional)\b|^\s+that\s+are\s+hidden\b", re.IGNORECASE)
_DEFAULT_BEFORE = re.compile(
    r"\b(?:default|serves)\s+(?:(?:MCP|core|tool)\s+)*(?:(?:surface|handshake|registry|set)\s+)?"
    r"(?:(?:is|are|exposes|stays\s+at|has)\s+)?$",
    re.IGNORECASE,
)
_DEFAULT_AFTER = re.compile(
    r"^(?:-tool\s+MCP\s+handshake\b|\s+(?:(?:MCP|core)\s+)*tools?\s+by\s+default\b|\s+by\s+default\b"
    r"|\s+default\s+tools?\b)",
    re.IGNORECASE,
)
_FULL_BEFORE = re.compile(r"\b(?:full|existing|past)\s+$", re.IGNORECASE)
# "The full surface has eleven." names the full count with no noun after it, and for digits too ("exposes all 11").
_FULL_SURFACE_VERB = re.compile(
    r"\bfull\s+(?:(?:core|MCP|tool)\s+)*(?:surface|set|registry|handshake|list)\s+"
    r"(?:is|are|has|have|exposes|lists|offers|serves|holds|gives|stays\s+at)\s+(?:all\s+)?$",
    re.IGNORECASE,
)
# "all seven" and "all three" name the things a sentence just listed, so `all` counts the surface only before a noun.
_ALL_BEFORE = re.compile(r"\ball\s+$", re.IGNORECASE)
_FULL_AFTER = re.compile(
    r"^(?:\s+with\s+`?ALICE_MCP_FULL_TOOLS\b|\s+plus\s+the\s+long\s+tail\b|-tool\s+core\b)",
    re.IGNORECASE,
)
# "`ALICE_MCP_FULL_TOOLS=1` exposes all eleven." names the full surface with no noun after the number. Up to three
# words may stand between the flag and the number ("`ALICE_MCP_FULL_TOOLS=1` also gives you all eleven"). The cues for
# the default and for the other tools are tried first.
_FULL_AFTER_THE_FLAG = re.compile(
    r"ALICE_MCP_FULL_TOOLS(?:=1)?`?\s+(?:[A-Za-z]+\s+){1,3}(?:all\s+)?$",
    re.IGNORECASE,
)

# Sentences that put a number beside a tool noun and count something other than the surface. Each entry is a pattern
# for the sentence and the reason it is not a surface count. `test_the_list_of_other_counts_holds_no_stale_entry` fails
# for an entry that no sentence in a living doc matches any longer.
_NOT_A_SURFACE_COUNT: tuple[tuple[str, str], ...] = (
    (
        r"\btools are read-only\b",
        "how many core tools are read-only and how many are destructive; the hints come from the registry and "
        "test_mcp_readonly_hints.py pins that sentence",
    ),
    (r"\bThese two tools match the query\b", "the two search tools, `alice_recall` and `alice_context_pack`"),
    (
        r"\bsuperseded carrier reproduced\b",
        "a measurement of a frozen release candidate in the security evidence package, true on its date",
    ),
    (
        r"\bMCP tools below read the continuity store\b",
        "how many legacy tools a legacy page lists below its header; test_audit_followup_docs.py checks each count "
        "against that page's own list",
    ),
    (
        r"\bMCP tools need `ALICE_LEGACY_SURFACES=1`",
        "how many task-brief tools the briefing page lists; test_audit_followup_docs.py checks that count",
    ),
    (
        r"\b(?:which reports nine tools|The nine tools were)\b",
        "the retired nine-tool surface the 2026-04-09 Hermes captures record; test_audit_followup_docs.py checks the "
        "count against the capture files",
    ),
)


def _value(token: str) -> int:
    lowered = token.lower()
    return int(lowered) if lowered.isdigit() else _COUNT_WORDS.index(lowered) + 1


def _role(token: str, left: str, right: str) -> str | None:
    """Which count a number is, read from the words around it: `others`, `default` or `full`; None when no cue.

    A cue that comes before the number is not enough for digits, which the pages use for parameter defaults
    (`max_items` has a default of 8). Digits count the surface only beside a tool noun, after a cue that follows them,
    after `full surface` and a verb, or after the flag and `all`.
    """

    clause_ends = _THEN_A_TOOL_COUNT_ENDS.search(right) is not None
    continues = clause_ends
    if token.isdigit():
        continues = continues and _BESIDE_A_TOOL_NOUN.search(right) is not None
    # Digits after the flag count the surface only as "all 11", never as a parameter ("lowers it to 5").
    after_the_flag = (
        clause_ends
        and _FULL_AFTER_THE_FLAG.search(left) is not None
        and (not token.isdigit() or _ALL_BEFORE.search(left) is not None)
    )
    if (continues and _OTHERS_BEFORE.search(left)) or _OTHERS_AFTER.search(right):
        return "others"
    if (continues and _DEFAULT_BEFORE.search(left)) or _DEFAULT_AFTER.search(right):
        return "default"
    beside_noun = _BESIDE_A_TOOL_NOUN.search(right) is not None
    if (
        (continues and _FULL_BEFORE.search(left))
        or (beside_noun and _ALL_BEFORE.search(left))
        or after_the_flag
        or (clause_ends and _FULL_SURFACE_VERB.search(left) is not None)
        or _FULL_AFTER.search(right)
    ):
        return "full"
    return None


def _sentences(text: str) -> list[tuple[str, str]]:
    """Each sentence of the text with the paragraph it sits in, both with their white space collapsed."""

    sentences: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n\s*\n", text):
        flat = " ".join(re.sub(r"(?m)^\s*>\s?", "", paragraph).split())
        sentences.extend((sentence, flat) for sentence in re.split(r"(?<=[.!?])\s+", flat))
    return sentences


def _claims(sentence: str, paragraph: str | None = None) -> list[tuple[str, int, str]]:
    """The counts a sentence gives: (role, number, the number as written). A role is `unclassified` for a number
    written beside a tool noun that no cue explains.

    The paragraph has to speak of tools, not the sentence alone: `The other eight are ...` follows the sentence that
    names the tools.
    """

    if not _ABOUT_THE_SURFACE.search(sentence if paragraph is None else paragraph):
        return []
    found: list[tuple[str, int, str]] = []
    for match in _COUNT_TOKEN.finditer(sentence):
        token = match.group("number")
        left = sentence[max(0, match.start() - 45) : match.start()]
        right = sentence[match.end() : match.end() + 60]
        role = _role(token, left, right)
        if role is None and _BESIDE_A_TOOL_NOUN.search(right):
            role = "unclassified"
        if role is not None:
            found.append((role, _value(token), token))
    return found


def _living_doc_claims() -> list[tuple[str, str, list[tuple[str, int, str]]]]:
    """(path, sentence, claims) for every sentence of every living doc that gives a count. Python files count under
    `docs/` only: the quickstart script's docstring is a copy of the sentence."""

    found: list[tuple[str, str, list[tuple[str, int, str]]]] = []
    for path in control_doc_truth.living_doc_files(ROOT, suffixes=(".md", ".py")):
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix == ".py" and not relative.startswith("docs/"):
            continue
        for sentence, paragraph in _sentences(path.read_text(encoding="utf-8")):
            claims = _claims(sentence, paragraph)
            if claims:
                found.append((relative, sentence, claims))
    return found


def _expected_counts() -> dict[str, int]:
    default = len(_DEFAULT_CORE_TOOL_ORDER)
    core = len(_CORE_TOOL_DEFINITIONS)
    return {"default": default, "full": core, "others": core - default}


def _allowed_other_count(sentence: str) -> bool:
    return any(re.search(pattern, sentence) for pattern, _reason in _NOT_A_SURFACE_COUNT)


def test_every_default_and_full_surface_count_in_the_living_docs_is_the_registry_count() -> None:
    expected = _expected_counts()
    wrong = [
        f"{relative}: says {written!r} for the {role} count, the registry lists {expected[role]}: {sentence[:140]}"
        for relative, sentence, claims in _living_doc_claims()
        for role, number, written in claims
        if role != "unclassified" and number != expected[role]
    ]

    assert wrong == []


def test_no_living_doc_gives_a_tool_count_in_a_shape_the_scan_does_not_know() -> None:
    unknown = [
        f"{relative}: {sentence[:160]}"
        for relative, sentence, claims in _living_doc_claims()
        if any(role == "unclassified" for role, _number, _written in claims) and not _allowed_other_count(sentence)
    ]

    assert unknown == [], (
        "These sentences put a number beside a tool noun and the scan cannot tell what it counts. Reword the "
        "sentence to name the default, the full or the other core tools, add its shape to the scan, or, when it "
        "counts something else, add it to _NOT_A_SURFACE_COUNT with the reason."
    )


def test_the_list_of_other_counts_holds_no_stale_entry() -> None:
    sentences = [
        sentence
        for _relative, sentence, claims in _living_doc_claims()
        if any(role == "unclassified" for role, _number, _written in claims)
    ]

    stale = [pattern for pattern, _reason in _NOT_A_SURFACE_COUNT if not any(re.search(pattern, s) for s in sentences)]
    assert stale == []
    assert all(reason for _pattern, reason in _NOT_A_SURFACE_COUNT)


def test_the_scan_reaches_the_pages_that_must_state_every_count() -> None:
    """The scan is not vacuous: the two tool guides state the default, the full and the other counts, and it finds
    claims across the living docs."""

    roles_by_page: dict[str, set[str]] = {}
    claim_count = 0
    for relative, _sentence, claims in _living_doc_claims():
        for role, _number, _written in claims:
            if role != "unclassified":
                claim_count += 1
                roles_by_page.setdefault(relative, set()).add(role)

    assert claim_count >= 25
    assert len(roles_by_page) >= 10
    for guide in ("docs/alpha/mcp-tools.md", "docs/integrations/mcp.md"):
        assert roles_by_page.get(guide) == {"default", "full", "others"}, guide


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MCP (3 default tools, 11 with ALICE_MCP_FULL_TOOLS=1)", [("default", 3), ("full", 11)]),
        ("Alice advertises three MCP tools by default.", [("default", 3)]),
        ("The default MCP handshake exposes three tools: a, b and c.", [("default", 3)]),
        ("Set `ALICE_MCP_FULL_TOOLS=1` to expose all eleven core tools.", [("full", 11)]),
        ("The default registry exposes three core MCP tools. `ALICE_MCP_FULL_TOOLS=1` exposes all eleven.", [("default", 3), ("full", 11)]),
        ("`ALICE_MCP_FULL_TOOLS=1` exposes all of them.", []),
        ("The other eight core tools stay defined.", [("others", 8)]),
        ("Set the flag to expose all eleven core tools. The other eight are `alice_capture`.", [("full", 11), ("others", 8)]),
        ("It does not imply the eight extra core tools.", [("others", 8)]),
        ("advertise all eleven core tools and accept calls to the eight that are hidden by default", [("full", 11), ("others", 8)]),
        ("(three by default, eleven with `ALICE_MCP_FULL_TOOLS=1`)", [("default", 3), ("full", 11)]),
        (
            "serves three MCP tools on a local SQLite file, or all eleven with `ALICE_MCP_FULL_TOOLS=1`",
            [("default", 3), ("full", 11)],
        ),
        ("62 legacy tools are listed (65 with the default three, 73 with the full eleven).", [("default", 3), ("full", 11)]),
        ("The default handshake exposes three of them.", [("default", 3)]),
        ("The core MCP surface stays small. The default handshake stays at three.", [("default", 3)]),
        ("Growing the core MCP surface past eleven tools.", [("full", 11)]),
        ("New tools need a reason the existing eleven cannot cover.", [("full", 11)]),
        ("The default loop has a three-tool MCP handshake.", [("default", 3)]),
        ("The full eleven-tool core surface.", [("full", 11)]),
        ("`ALICE_MCP_FULL_TOOLS=1` offers all twelve.", [("full", 12)]),
        ("`ALICE_MCP_FULL_TOOLS=1` also gives you all twelve.", [("full", 12)]),
        ("With `ALICE_MCP_FULL_TOOLS=1` the server lists all 11.", [("full", 11)]),
        ("The full surface has twelve.", [("full", 12)]),
        ("The full core registry exposes all eleven.", [("full", 11)]),
        ("The full core registry exposes all 12.", [("full", 12)]),
        ("# default three plus the long tail (65), or 68 with ALICE_LEGACY_SURFACES=1 to the tools", [("default", 3)]),
        # A number beside a tool noun that no cue explains is reported, not skipped.
        ("The server lists twelve core tools.", [("unclassified", 12)]),
        ("It lists four tools.", [("unclassified", 4)]),
        # The words "full tool set" before a count of something else do not make it the full count.
        ("With the full tool set six tools are read-only.", [("unclassified", 6)]),
        # A count of something else is not read as a count of the surface.
        ("Tools: all three doors hold a cited source.", []),
        ("Tools: the commit text, and the other seven verbs, need `ALICE_MCP_FULL_TOOLS=1`.", []),
        ("The tool takes a limit (default 8) and a depth (default 5).", []),
        ("Tools: the table lists all seven. The hook keeps all three, and the tool is read-only.", []),
        ("The default retention of the tool is 14 days.", []),
        ("`ALICE_MCP_FULL_TOOLS=1` turns the tools on in v0.20.0.", []),
        ("`ALICE_MCP_FULL_TOOLS=1` lowers it to 5.", []),
        ("With `ALICE_MCP_FULL_TOOLS=1` the default three become the full set.", []),
    ],
)
def test_the_scan_classifies_the_shapes_the_docs_use(text: str, expected: list[tuple[str, int]]) -> None:
    found = [
        (role, number)
        for sentence, paragraph in _sentences(text)
        for role, number, _written in _claims(sentence, paragraph)
    ]
    assert found == expected
