"""The permission profiles and the held-back domains the docs name are the ones the policy engine uses.

`docs/alpha/mcp-tools.md`, `docs/alpha/security-and-privacy.md` and `docs/alpha/agent-integration.md` each say which
permission profiles read every domain and which five domains every other profile is held back from. The sentences are
short and each sits where its reader needs it, but nothing tied them to `RESTRICTED_DOMAINS`,
`UNRESTRICTED_DOMAIN_PROFILES` or `PERMISSION_PROFILES`, so a change to the constants would leave the pages agreeing
with each other and wrong.

Mutations, each one alone: add a sixth label to `RESTRICTED_DOMAINS`; drop `financial` from the sentence in
`docs/alpha/agent-integration.md`; change `five` to `six` in `docs/alpha/security-and-privacy.md`; add a sixth name to
`PERMISSION_PROFILES`. Each fails one of the tests below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alicebot_api.vnext_agent_control import (
    PERMISSION_PROFILES,
    RESTRICTED_DOMAINS,
    UNRESTRICTED_DOMAIN_PROFILES,
    VNEXT_DOMAINS,
)

ROOT = Path(__file__).resolve().parents[2]

_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


def _flat(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def _domains_section() -> str:
    page = _flat("docs/alpha/mcp-tools.md")
    start = page.index("## Domains a profile may read ")
    end = page.index(" ## ", start + 1)
    return page[start:end]


def _labels_in(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z_]+", text) if word in VNEXT_DOMAINS}


def _names_in(text: str) -> set[str]:
    return set(re.findall(r"`([a-z_]+)`", text))


# (page, the text to read, the sentence pattern). Group 1 names the profiles, the last group lists the domains.
_COUNTED_SENTENCE = re.compile(
    r"every permission profile except (.+?) is held back from (\w+) domains: (.+?)\.(?: |$)", re.IGNORECASE
)


@pytest.mark.parametrize("page", ("mcp-tools", "security-and-privacy"))
def test_the_held_back_sentence_names_the_profiles_the_domains_and_the_count(page: str) -> None:
    text = _domains_section() if page == "mcp-tools" else _flat("docs/alpha/security-and-privacy.md")
    match = _COUNTED_SENTENCE.search(text)
    assert match is not None, f"{page} no longer has the sentence that says which profiles are held back"

    assert _names_in(match.group(1)) == set(UNRESTRICTED_DOMAIN_PROFILES)
    assert match.group(2) == _NUMBER_WORDS[len(RESTRICTED_DOMAINS)]
    assert _labels_in(match.group(3)) == set(RESTRICTED_DOMAINS)


def test_the_agent_integration_sentence_names_the_profiles_and_the_domains() -> None:
    text = _flat("docs/alpha/agent-integration.md")
    match = re.search(r"Every profile except (.+?) is held back from the (.+?) domains\.", text)
    assert match is not None, "agent-integration.md no longer has the sentence that says which profiles are held back"

    assert _names_in(match.group(1)) == set(UNRESTRICTED_DOMAIN_PROFILES)
    assert _labels_in(match.group(2)) == set(RESTRICTED_DOMAINS)


@pytest.mark.parametrize("relative", ("docs/alpha/agent-integration.md", "docs/alpha/mcp-tools.md"))
def test_the_pages_that_describe_the_profiles_name_every_profile(relative: str) -> None:
    named = _names_in(_flat(relative))
    missing = sorted(set(PERMISSION_PROFILES) - named)
    assert missing == [], f"{relative} does not name {missing}"
