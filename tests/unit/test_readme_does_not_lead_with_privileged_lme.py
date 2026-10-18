"""README must not lead with the privileged LongMemEval number as current product.

81.2% is a v0.12.0 store_chunks receipt. The badge and the first LongMemEval
paragraph used to present it as the product path. The named mutation is
restoring the old ``LongMemEval_s-81.2% (3-run mean)`` badge as the lead
claim. That change must fail this test.

The README carries no LongMemEval badge at all until the product path has a
published score: the receipt is described in the benchmark section with its
known issue, not advertised above the fold. Restoring any LongMemEval badge in
the lead must fail ``test_readme_lead_has_no_longmemeval_badge``.
"""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"

SCORE = "81.2"


def _lead(readme: str) -> str:
    match = re.search(r"^## ", readme, flags=re.MULTILINE)
    return readme if match is None else readme[: match.start()]


def _lme_badge_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if "LongMemEval" in line and line.lstrip().startswith("[![")
    ]


def _first_lme_paragraph(text: str) -> str | None:
    for block in re.split(r"\n\s*\n", text):
        if "LongMemEval" not in block:
            continue
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and all(line.startswith("![") or line.startswith("[![") for line in lines):
            continue
        return block
    return None


def _names_harness_and_tag(unit: str) -> bool:
    return "store_chunks" in unit and "v0.12.0" in unit


def test_readme_does_not_lead_with_privileged_lme_as_product_path() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    lead = _lead(readme)

    for badge in _lme_badge_lines(lead):
        if SCORE in badge:
            assert _names_harness_and_tag(badge), (
                "README badge presents 81.2% as the current-release product "
                "path without naming store_chunks / v0.12.0"
            )

    paragraph = _first_lme_paragraph(readme)
    if paragraph is not None and SCORE in paragraph:
        assert _names_harness_and_tag(paragraph), (
            "first LongMemEval paragraph presents 81.2% as the current-release "
            "product path without naming store_chunks / v0.12.0"
        )


def test_readme_lead_has_no_longmemeval_badge() -> None:
    """Mutation: put any LongMemEval badge back above the first section heading."""

    lead = _lead(README_PATH.read_text(encoding="utf-8"))
    assert _lme_badge_lines(lead) == []
    assert "img.shields.io/badge/LongMemEval" not in lead
