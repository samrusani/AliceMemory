"""The README is short and keeps no version history, and the facts it dropped live in the docs.

The README describes the latest release and nothing else. Its older versions of a rule ("From v0.19.0 ... In v0.18.0
...") are in the changelog and the release notes, and the current rules it used to retell live in the page that covers
them. Each test names the edit that must fail it. The pins on the README's own sentences stay in the test files that
guarded them: the plugin, OpenCode, control document, embeddings and LongMemEval tests.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
README_CEILING = 13_000


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _flat(relative: str) -> str:
    return " ".join(_text(relative).split())


def test_the_readme_stays_short() -> None:
    """Mutation: paste a retired paragraph back (the version history, the skill pack steps, the Postgres
    MCP config) until the file passes the ceiling.
    """

    assert len(_text("README.md")) <= README_CEILING


def test_the_readme_keeps_no_version_history_and_one_from_v0210_line() -> None:
    """Mutation: write a ``From v0.19.0``, ``From v0.20.0`` or ``In v0.18.0`` sentence into README.md, put the
    ``Unreleased (on main`` marker back, name a second behaviour that begins in v0.21.0, or drop the one line
    about ``alice_what_changed``.
    """

    readme = _text("README.md")
    assert not [line for line in readme.splitlines() if re.match(r"(From|In|Since) v0\.\d+\.\d+\b", line)]
    assert not re.search(r"\b(?:From|In) v0\.(?:1[0-9]|20)\.\d+[ ,]", readme)
    assert "Unreleased (on main" not in readme
    marked = [line for line in readme.splitlines() if "From v0.21.0" in line]
    assert len(marked) == 1
    assert marked[0].count("From v0.21.0") == 1
    assert "From v0.21.0, `alice_what_changed` is one of the other nine core tools" in marked[0]


def test_the_readme_security_wording_is_unchanged() -> None:
    """Mutation: reword the posture, or say anyone outside the project looked at the code."""

    readme = _flat("README.md")
    assert (
        "Security posture: automated security scanning and internal adversarial review, findings triaged and fixed. "
        "No one outside the project has audited the code."
    ) in readme


def test_the_readme_names_the_default_tools_and_the_package_runtime() -> None:
    """Mutation: drop one of the three default tools, the OpenClaw prefix sentence, or the install note."""

    readme = _text("README.md")
    for tool in ("alice_memory_commit", "alice_recall", "alice_resume"):
        assert f"- `{tool}`:" in readme
    assert "OpenClaw prefixes MCP tool names with the server name, so `alice_recall` reaches the model as `alice__alice_recall`." in readme
    assert "The name `alice-core` on PyPI belongs to an unrelated project." in readme
    assert "To run v0.20.2, run `git checkout v0.20.2` before `make setup`." in readme


def test_the_skill_pack_steps_moved_to_the_agent_integration_page() -> None:
    """Mutation: delete the section, one of its two copy commands, or the sentence that says no test measures a
    pack; put the ``cp -R`` commands back into README.md; drop the README's link to the section.
    """

    page = _text("docs/alpha/agent-integration.md")
    assert "\n## Skill packs\n" in page
    flat = _flat("docs/alpha/agent-integration.md")
    assert "Packs exist for 2 of the 7 install hosts, Hermes and OpenClaw." in flat
    assert "Nothing in this repo measures whether a pack changes what an agent does." in flat
    assert "cp -R agent-skills/openclaw/alice-project-memory ~/.openclaw/skills/" in page
    assert "cp -R agent-skills/hermes/alice-memory ~/.hermes/skills/" in page
    assert "Copy the directory, not the file:" in flat
    assert "An older Hermes pack at `docs/integrations/hermes-skill-pack/skills/alice-workflows/` is legacy." in flat
    assert "A skill grants no tools on its own" in flat
    assert (ROOT / "agent-skills/hermes/alice-memory/SKILL.md").is_file()
    assert (ROOT / "agent-skills/openclaw/alice-project-memory/SKILL.md").is_file()

    readme = _text("README.md")
    assert "cp -R" not in readme and "Skill packs" not in readme
    assert "(docs/alpha/agent-integration.md#skill-packs)" in readme


def test_the_openclaw_one_line_add_moved_to_the_openclaw_page() -> None:
    """Mutation: delete the command or the probe sentence from the OpenClaw page, or put the command back
    into README.md.
    """

    page = _text("docs/integrations/openclaw.md")
    assert "OpenClaw can also add the server in one line, which probes before saving:" in page
    assert "openclaw mcp add alice --command uvx --arg alice-memory --arg mcp --arg --data-dir --arg ~/.alice" in page
    assert "openclaw mcp add" not in _text("README.md")


def test_the_embeddings_behaviour_lives_on_the_tools_page() -> None:
    """Mutation: delete a sentence below from ``docs/alpha/mcp-tools.md``.

    The page holds what the endpoint receives (only memories recall can return), the cap and its option, the
    refused-memory report and the doctor count. The README keeps the cap in one sentence and links the section.
    """

    page = _flat("docs/alpha/mcp-tools.md")
    for sentence in (
        "Alice sends the text of a memory to the embeddings endpoint only when recall can return it, which means "
        "its status is active or accepted and its `valid_to`, if it has one, has not passed.",
        "Each is embedded once, when it becomes active (confirm, approve, correct), and a rejected one is never sent.",
        "use the same test, so a forgotten, rejected, candidate or expired memory is not sent or counted",
        "Text already sent cannot be recalled from the endpoint.",
        "Each memory and each recall query is cut to its first 8,000 characters before it is sent",
        "`ALICE_EMBEDDINGS_MAX_INPUT_CHARS` (256 to 1,000,000) changes the cap",
        "lists the failed memory ids and the endpoint's reason (at most 300 characters, withheld if it looks like a credential)",
    ):
        assert sentence in page, sentence
    assert "(docs/alpha/mcp-tools.md#embeddings)" in _text("README.md")


def test_the_install_page_gives_the_host_flags_for_the_defaults_and_hermes() -> None:
    """Mutation: delete the example flags from the quickstart's Hermes line."""

    assert (
        "`--host claude-desktop --host claude-code --host cursor --host openclaw --host hermes` "
        "for the four defaults and Hermes"
    ) in _flat("docs/alpha/quickstart.md")


def test_the_postgres_mcp_config_lives_in_the_agent_integration_page_not_the_readme() -> None:
    """Mutation: delete the config from the agent integration page, or paste it back into README.md."""

    page = _text("docs/alpha/agent-integration.md")
    assert '"args": ["-m", "alicebot_api.mcp_server"]' in page
    assert '"DATABASE_URL": "postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot"' in page
    readme = _text("README.md")
    assert "alicebot_api.mcp_server" not in readme and "alicebot_app" not in readme
    assert "use the config in [agent integration](docs/alpha/agent-integration.md)" in readme


def test_the_never_published_tags_are_listed_outside_the_readme() -> None:
    """Mutation: delete a tag from the state file or the roadmap, or list the tags in README.md again."""

    tags = ("v0.9.3", "v0.10.0", "v0.13.0", "v0.15.0", "v0.19.1", "v0.20.1")
    for name in ("CURRENT_STATE.md", "ROADMAP.md"):
        flat = _flat(name)
        for tag in tags:
            assert f"`{tag}`" in flat, (name, tag)
    readme = _text("README.md")
    assert "(CURRENT_STATE.md)" in readme
    assert not [tag for tag in tags if tag in readme]


def test_the_install_details_the_readme_dropped_stay_on_their_pages() -> None:
    """Mutation: delete one of these sentences from its page.

    The README keeps the default hosts, the SessionStart hook, the backup and ``--dry-run``. The pages hold the
    rest: the launcher fallback, what a re-run keeps, the Hermes env rule and the Codex trust step.
    """

    quickstart = _flat("docs/alpha/quickstart.md")
    assert "Without uv: when `uvx` is not on PATH, install writes the absolute path of the installed `alice-memory` script" in quickstart
    assert "A re-run keeps keys you added to an existing Alice entry" in quickstart
    assert "Without `--data-dir`, install keeps the data dir an existing Alice entry uses, else `~/.alice`." in quickstart
    assert "It does not import a vault or create the database." in quickstart
    assert "`alice-memory install --host hermes` keeps a documented Alice env value" in _flat("docs/integrations/hermes.md")
    assert "Hooks need review" in _flat("docs/integrations/codex.md")
