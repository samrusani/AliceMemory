"""Control docs must not disagree with each other about which release is current.

This is the guard for an accretion pattern that ran for at least four releases
before anyone noticed, and that `check_control_doc_truth.py` passes cleanly.

The existing checker asks whether the current version is *named* in each control
doc, and whether required headings exist. It does not ask whether the sentences
around those names contradict one another. So each release appended its own
"X is the prior published release" line without retiring the previous one, and
the documents accumulated mutually exclusive claims:

    CURRENT_STATE.md   v0.12.0 AND v0.11.1 both "the prior published release"
    ARCHITECTURE.md    v0.12.0 AND v0.10.4  both "the prior published release"
    PRODUCT_BRIEF.md   v0.13.1              "the prior published release"

All while the actual prior release was v0.15.5. Found by review on 2026-08-16,
in three separate passes, because fixing one file did not surface the others.

A related failure the same day: a mechanical version rename produced three
identical `## What v0.15.6 Shipped` headings in one file, and the checker passed
because it only requires `## Snapshot`, `## Release Boundary` and
`## Product Boundaries` to be present.

**What this asserts is agreement, not correctness.** It cannot know which release
is genuinely latest; `check_control_doc_truth.py` owns that. It asserts that the
documents tell the same story, which is the property that silently rotted.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# The docs that describe the CURRENT release baseline. Release notes and archived
# handoffs are excluded on purpose: they describe the release they were written
# for and are meant to keep naming it.
CONTROL_DOCS = (
    "CURRENT_STATE.md",
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "README.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

_VERSION = r"`(v\d+\.\d+\.\d+)`"
LATEST_CLAIM = re.compile(_VERSION + r" is the latest published release")
PRIOR_CLAIM = re.compile(_VERSION + r" is the (?:immediately )?prior published release")


def _collapsed(relative_path: str) -> str:
    """Whole-file text with newlines collapsed, so wrapped sentences still match."""

    return " ".join((REPO_ROOT / relative_path).read_text(encoding="utf-8").split())


def _claims(pattern: re.Pattern[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for relative_path in CONTROL_DOCS:
        for version in pattern.findall(_collapsed(relative_path)):
            found[relative_path].add(version)
    return dict(found)


def test_the_control_docs_exist() -> None:
    """Guards against the whole check silently covering nothing."""

    for relative_path in CONTROL_DOCS:
        assert (REPO_ROOT / relative_path).is_file(), f"{relative_path} is missing"


def test_every_doc_names_the_same_latest_release() -> None:
    claims = _claims(LATEST_CLAIM)
    assert claims, "no document claims a latest published release; the pattern may have drifted"

    versions = {version for names in claims.values() for version in names}
    assert len(versions) == 1, (
        "control docs disagree about the latest published release: "
        + "; ".join(f"{path} says {sorted(names)}" for path, names in sorted(claims.items()))
    )


def test_every_doc_names_the_same_prior_release() -> None:
    claims = _claims(PRIOR_CLAIM)
    assert claims, "no document claims a prior published release"

    versions = {version for names in claims.values() for version in names}
    assert len(versions) == 1, (
        "control docs disagree about the prior published release, which is the "
        "accretion this guard exists for: "
        + "; ".join(f"{path} says {sorted(names)}" for path, names in sorted(claims.items()))
    )


def test_no_single_doc_names_two_different_prior_releases() -> None:
    """The precise shape of the bug: one file carrying several stale claims."""

    for relative_path, versions in _claims(PRIOR_CLAIM).items():
        assert len(versions) == 1, (
            f"{relative_path} calls {sorted(versions)} the prior published release. "
            "A release bump appended a claim without retiring the previous one."
        )


def test_the_prior_release_is_not_also_the_latest() -> None:
    latest = {v for names in _claims(LATEST_CLAIM).values() for v in names}
    prior = {v for names in _claims(PRIOR_CLAIM).values() for v in names}

    assert not (latest & prior), (
        f"{sorted(latest & prior)} is described as both the latest and the prior "
        "published release"
    )


@pytest.mark.parametrize("relative_path", CONTROL_DOCS)
def test_no_duplicated_headings_within_a_document(relative_path: str) -> None:
    """A mechanical version rename collapsed three distinct sections into one name.

    `check_control_doc_truth.py` passed throughout, because it checks that certain
    headings are present and never that they are unique.
    """

    body = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    headings = re.findall(r"(?m)^(#{2,3} .+)$", body)

    duplicates = sorted({h for h in headings if headings.count(h) > 1})
    assert not duplicates, (
        f"{relative_path} repeats {duplicates}. A version rename most likely "
        "collapsed distinct sections into the same heading."
    )
