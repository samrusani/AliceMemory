"""Keep the public statements in step with supported platforms and behavior."""

import ast
from importlib.util import resolve_name
from pathlib import Path
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "apps/api/src"
PLATFORMS = "Alice runs on Linux and macOS, and Windows is not supported for now."
COMMIT = "If both calls are accepted, a commit sent twice without an `idempotency_key` is stored twice."
REVIEW = (
    "A second internal review is still being worked through: v0.25.0 carries its first fixes, "
    "and some of its findings are not fixed yet. No one outside the project has audited the code."
)
SKILLS = (
    "agent-skills/hermes/alice-memory/SKILL.md",
    "agent-skills/openclaw/alice-project-memory/SKILL.md",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _section(page: str, heading: str, *, level: str = "##") -> str:
    section = _text(page).split(f"\n{level} {heading}\n", 1)[1].split(f"\n{level} ", 1)[0]
    return " ".join(section.split())


def test_security_policy_states_the_review_status() -> None:
    """Mutation: remove the sentence about the second review from Security Review."""
    assert _section("SECURITY.md", "Security Review") == (
        f"Alice's security work is automated security scanning and internal adversarial review. {REVIEW} "
        "From v0.15.1 on, each version's release notes list its known gaps."
    )


def test_security_and_privacy_page_states_the_review_status() -> None:
    """Mutation: replace the current review statement with the historical claim."""
    page = " ".join(_text("docs/alpha/security-and-privacy.md").split())
    assert f"Alice's security work is automated security scanning and internal adversarial review. {REVIEW}" in page


@pytest.mark.parametrize("page", ("README.md", "external-review-brief.md", "stage-a-evidence.md"))
def test_security_evidence_dates_the_old_wording_and_states_the_current_review(page: str) -> None:
    """Mutation for each page: remove the date from the preserved claim."""
    text = _text(f"docs/security/{page}")
    historical, current = text.split("The current public security statement", 1)
    assert "recorded on 2026-10-04" in " ".join(historical.split())
    assert "automated security scanning and internal adversarial review, findings triaged and fixed." in " ".join(
        historical.split()
    )
    assert REVIEW in " ".join(current.split())


def test_security_policy_describes_what_reaches_the_reranker() -> None:
    """Mutation: replace source titles with source summaries in the disclosure."""
    section = _section("SECURITY.md", "What Text Leaves the Machine")
    reranker = section.split("- The context-pack reranker", 1)[1].split(" - ", 1)[0]
    assert reranker == (
        " is off until both `ALICE_RERANKER_BASE_URL` and `ALICE_RERANKER_MODEL` are set. "
        "Its OpenAI-compatible endpoint may be remote. It receives the query with the text of up to 48 candidate "
        "memories and, separately, the query with the titles of up to 24 candidate sources per pack. Source summaries, "
        "raw text, passages and titles outside the caller's read access stay out of these requests. Credential-like text "
        "in a readable title can be sent with the title. If the endpoint disconnects, the pack still builds and its trace "
        "records `fail_open`."
    )


@pytest.mark.parametrize(
    ("page", "heading"),
    [
        ("README.md", "Quickstart"),
        ("docs/alpha/quickstart.md", "Install with alice-memory"),
        ("docs/alpha/known-limitations.md", None),
    ],
    ids=("readme", "quickstart", "limitations"),
)
def test_pages_state_the_supported_platforms(page: str, heading: str | None) -> None:
    """Mutation for each page: remove its sentence about Windows support."""
    text = _section(page, heading) if heading else _text(page)
    assert PLATFORMS in text


def _module_file(module: str) -> Path | None:
    base = API.joinpath(*module.split("."))
    for path in (base / "__init__.py", base.with_suffix(".py")):
        if path.is_file():
            return path
    return None


def _fcntl_imports_reachable_from(entry: str) -> set[str]:
    pending = [entry]
    seen: set[str] = set()
    blocked: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen or not module.startswith("alicebot_api"):
            continue
        seen.add(module)
        path = _module_file(module)
        if path is None:
            continue
        parent = module.rpartition(".")[0]
        if parent:
            pending.append(parent)
        package = module if path.name == "__init__.py" else parent
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
                if "fcntl" in imports:
                    blocked.add(module)
                pending.extend(imports)
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
                if base == "fcntl":
                    blocked.add(module)
                if base:
                    pending.append(base)
                    pending.extend(f"{base}.{alias.name}" for alias in node.names)
    return blocked


@pytest.mark.parametrize("path", SKILLS, ids=("hermes", "openclaw"))
def test_skill_platforms_respect_console_entry_imports(path: str) -> None:
    """Mutation for either skill: add Windows to its platforms while the entry imports fcntl."""
    front_matter = _text(path).split("---", 2)[1]
    platforms = yaml.safe_load(front_matter)["platforms"]
    assert {"linux", "macos"}.issubset(platforms)
    scripts = tomllib.loads(_text("pyproject.toml"))["project"]["scripts"]
    blocked = {
        script: sorted(modules)
        for script, entry in scripts.items()
        if (modules := _fcntl_imports_reachable_from(entry.split(":", 1)[0]))
    }
    assert "windows" not in platforms or not blocked, blocked


@pytest.mark.parametrize(
    ("page", "heading"),
    [
        ("docs/alpha/known-limitations.md", None),
        ("docs/memory-operations-protocol.md", "remember"),
        ("docs/alpha/mcp-tools.md", "Explicit memory commits"),
    ],
    ids=("limitations", "protocol", "mcp-tools"),
)
def test_pages_state_the_effect_of_repeating_a_commit_without_a_key(page: str, heading: str | None) -> None:
    """Mutation for each page: remove the sentence about accepted commits without a key."""
    text = _section(page, heading) if heading else _text(page)
    assert COMMIT in text
    if heading is None:
        bullets = [line for line in text.splitlines() if line.startswith("- ")]
        current = next(index for index, bullet in enumerate(bullets) if COMMIT in bullet)
        assert bullets[current - 1].startswith("- idempotency keys are shared across the vault")
        assert "Send a key with every commit to cover retries after a lost reply or duplicate delivery." in bullets[current]
