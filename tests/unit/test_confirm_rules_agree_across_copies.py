"""The rules for answering a pending write read the same in every copy, and they are the rules the server applies.

The rules are written once, in "Confirm and reject rules" of `docs/memory-operations-protocol.md`. Six other
files state some of them, because an agent is given only the skill pack (`agent-skills/hermes/alice-memory/SKILL.md`,
`agent-skills/openclaw/alice-project-memory/SKILL.md`) and the pages next to it (`docs/alpha/hermes-skill.md`,
`docs/alpha/openclaw-skill.md`), and an integrator reads the tool reference (`docs/alpha/mcp-tools.md`) or the
agent integration guide (`docs/alpha/agent-integration.md`). Two tool descriptions in the registry say it too, and
those are what an agent reads in `tools/list`.

This test reads every copy and compares what it says with the canonical section and with the server:

* who may confirm or reject: the author, an `admin_agent` key, the owner, and nobody else; and that on a keyless
  install the author limit is not protection;
* who is exempt from the sensitivity ceiling: the owner, an `admin_agent` key and a keyless call that declares
  `permission_profile: admin_agent`; and that the author can still reject its own pending write above the ceiling;
* the expiry in hours, read from `CONFIRMATION_EXPIRY_HOURS`, never typed here;
* what a refused stdio caller gets: the code and the fixed message, and the main-only codes under their marker.

The sets of who may answer and who is exempt are also derived from the server by running real callers, so a copy
that agrees with the others and disagrees with the code fails too. A last check finds any other living file that
states these rules and asks for it to be registered here.

Mutations, each one alone (every one fails a test below):

* in `docs/alpha/mcp-tools.md`, change `an `admin_agent` key, or the owner can confirm` to `a `trusted_local_agent`
  key, or the owner can confirm`; delete `the owner` from the exemption sentence of `docs/alpha/agent-integration.md`;
* in `agent-skills/openclaw/alice-project-memory/SKILL.md`, change `After 24 hours` to `After 12 hours`; in
  `docs/alpha/hermes-skill.md`, change `After 24 hours` to `After 48 hours`;
* set `CONFIRMATION_EXPIRY_HOURS` to 12 in `vnext_memory_commit.py` (the docs then disagree with it);
* in the protocol page, change `not_permitted` to `not_found` in the stdio sentence, or drop the keyless caveat
  from any one copy;
* in `vnext_memory_commit.py`, let any caller confirm (the server then disagrees with every doc).
"""

from __future__ import annotations

import json
import os
import re
from io import BytesIO
from pathlib import Path

import pytest

from alicebot_api import mcp_server
from alicebot_api.vnext_memory_commit import CONFIRMATION_EXPIRY_HOURS
from tests.unit.test_default_surface_can_finish_confirmation_required import (  # noqa: F401  (fixture)
    _commit_pending,
    _context,
    _mint_key,
    default_surface,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every file that states the rules, and the rules each one must state. A rule a copy does not need to state is not
# listed for it: the skill packs tell an agent what to do and leave out the exemptions an agent cannot use.
WHO, KEYLESS, EXEMPT, REJECT_ABOVE, EXPIRY, STDIO = "who", "keyless", "exempt", "reject_above", "expiry", "stdio"

DOCS: dict[str, tuple[str, frozenset[str]]] = {
    "protocol": (
        "docs/memory-operations-protocol.md",
        frozenset({WHO, KEYLESS, EXEMPT, REJECT_ABOVE, EXPIRY, STDIO}),
    ),
    "tool reference": ("docs/alpha/mcp-tools.md", frozenset({WHO, KEYLESS, EXEMPT, EXPIRY, STDIO})),
    "integration guide": ("docs/alpha/agent-integration.md", frozenset({WHO, KEYLESS, EXEMPT, EXPIRY})),
    "hermes skill pack": (
        "agent-skills/hermes/alice-memory/SKILL.md",
        frozenset({WHO, KEYLESS, REJECT_ABOVE, EXPIRY}),
    ),
    "openclaw skill pack": (
        "agent-skills/openclaw/alice-project-memory/SKILL.md",
        frozenset({WHO, KEYLESS, EXPIRY}),
    ),
    "hermes skill page": ("docs/alpha/hermes-skill.md", frozenset({WHO, KEYLESS, REJECT_ABOVE, EXPIRY})),
    "openclaw skill page": ("docs/alpha/openclaw-skill.md", frozenset({WHO, KEYLESS, EXPIRY})),
}
# What an agent reads in `tools/list`: (tool name, property or None for the tool description) and the rules it states.
TOOL_COPIES: dict[str, tuple[str, str | None, frozenset[str]]] = {
    "alice_memory_commit confirmation_action": (
        "alice_memory_commit",
        "confirmation_action",
        frozenset({WHO, KEYLESS, REJECT_ABOVE, EXPIRY}),
    ),
    "alice_memory_manage description": ("alice_memory_manage", None, frozenset({WHO, KEYLESS})),
}

AUTHOR = {"agent_id": "hermes", "agent_type": "personal_assistant", "permission_profile": "trusted_local_agent"}
WHO_MAY_ANSWER = frozenset({"author", "admin_agent key", "owner"})
EXEMPT_FROM_CEILING = frozenset({"owner", "admin_agent key", "keyless call that declares admin_agent"})
OTHER_PROFILES = (
    "trusted_local_agent",
    "project_scoped_agent",
    "read_only_agent",
    "memory_proposal_agent",
)
FIXED_MESSAGE = "The tool request could not be processed"
MARK = "Unreleased (on main, not in v0.20.0):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _doc_text(label: str) -> str:
    return _flat((REPO_ROOT / DOCS[label][0]).read_text(encoding="utf-8"))


def _tool_text(label: str) -> str:
    from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS

    name, prop, _rules = TOOL_COPIES[label]
    tool = next(tool for tool in _CORE_TOOL_DEFINITIONS if tool["name"] == name)
    if prop is None:
        return _flat(str(tool["description"]))
    return _flat(str(tool["inputSchema"]["properties"][prop]["description"]))  # type: ignore[index]


def _copies() -> list[tuple[str, str, frozenset[str]]]:
    found = [(label, _doc_text(label), DOCS[label][1]) for label in DOCS]
    found += [(label, _tool_text(label), TOOL_COPIES[label][2]) for label in TOOL_COPIES]
    return found


# --- reading the rules out of a copy -----------------------------------------------------------------------------


def _callers_named(fragment: str) -> frozenset[str]:
    """The callers a fragment names, with any profile it names that is not allowed to answer."""

    named: set[str] = set()
    if re.search(r"\bauthor(?:ed)?\b", fragment):
        named.add("author")
    if re.search(r"`?admin_agent`? key", fragment):
        named.add("admin_agent key")
    if re.search(r"\bowner\b", fragment):
        named.add("owner")
    named.update(profile for profile in OTHER_PROFILES if profile in fragment)
    return frozenset(named)


def _who_may_answer(text: str) -> list[frozenset[str]]:
    """Every sentence in the copy that says who may confirm or reject, as the set of callers it names."""

    said = [_callers_named(m.group("who")) for m in re.finditer(r"[Oo]nly (?P<who>[^.]*?) can confirm or reject", text)]
    said += [
        _callers_named(m.group("who"))
        for m in re.finditer(r"limited to (?P<who>its author, an `?admin_agent`? key, or the owner)", text)
    ]
    return said


def _exemptions(text: str) -> list[frozenset[str]]:
    """Every sentence that lists who is not held to the ceiling, as the set of callers it names."""

    said: list[frozenset[str]] = []
    for match in re.finditer(
        r"(?P<who>[Tt]he owner[^.]*?)\s+(?:are not held to that ceiling|still get `confirmation_required`)", text
    ):
        fragment = match.group("who")
        named: set[str] = set()
        if re.search(r"\bowner\b", fragment):
            named.add("owner")
        without_declared = fragment.replace("declares `permission_profile: admin_agent`", "")
        if re.search(r"`admin_agent` key", without_declared):
            named.add("admin_agent key")
        if "keyless call that declares `permission_profile: admin_agent`" in fragment:
            named.add("keyless call that declares admin_agent")
        named.update(profile for profile in OTHER_PROFILES if profile in fragment)
        said.append(frozenset(named))
    return said


def _expiry_hours(text: str) -> list[int]:
    """Every number of hours the copy gives for a pending confirmation, found by the words around it."""

    hours: list[int] = []
    for match in re.finditer(r"\b(\d+)[ -]hours?\b", text):
        around = text[max(0, match.start() - 200) : match.end() + 200].lower()
        if "confirm" in around or "pending" in around or "expire" in around:
            hours.append(int(match.group(1)))
    return hours


# --- the copies agree with one another and with the constant ---------------------------------------------------


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if WHO in rules])
def test_every_copy_names_the_same_callers_for_who_may_answer(label: str) -> None:
    """The author, an `admin_agent` key and the owner, and nobody else, in every copy that says who may answer.

    Mutations: name another profile (``a `trusted_local_agent` key``) or leave out the owner in one copy; drop the
    sentence from one copy.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    said = _who_may_answer(text)
    assert said, f"{label} no longer says who may confirm or reject a pending write"
    for named in said:
        assert named == WHO_MAY_ANSWER, (label, sorted(named))


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if KEYLESS in rules])
def test_every_copy_says_the_author_limit_is_not_protection_on_a_keyless_install(label: str) -> None:
    """On a keyless install the caller can declare the author's agent_id, so the limit protects nothing.

    Mutation: delete the sentence from one copy, or say a keyless server verifies the author.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    assert re.search(r"[Oo]n a keyless install,? that limit is not protection", text), label
    assert "declare the author's agent_id" in text, label
    assert "a keyless server verifies" not in text, label


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if EXEMPT in rules])
def test_every_copy_names_the_same_callers_as_exempt_from_the_ceiling(label: str) -> None:
    """The owner, an `admin_agent` key and a keyless call that declares `permission_profile: admin_agent`.

    Mutations: delete one of the three from one copy, or add `trusted_local_agent` to the list.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    said = _exemptions(text)
    assert said, f"{label} no longer says who is not held to the sensitivity ceiling"
    for named in said:
        assert named == EXEMPT_FROM_CEILING, (label, sorted(named))


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if REJECT_ABOVE in rules])
def test_every_copy_that_says_it_lets_the_author_reject_above_the_ceiling(label: str) -> None:
    """The author can still reject its own pending write above the ceiling, and no copy says it cannot.

    Mutation: delete the sentence from one copy, or say the author cannot reject it.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    assert re.search(
        r"can (?:still )?reject (?:their|your|its) own pending write(?: even when it is)? above (?:the|that|their)"
        r"(?: sensitivity)? ceiling",
        text,
    ), label
    assert not re.search(r"cannot reject (?:their|your|its) own", text), label


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if EXPIRY in rules])
def test_every_copy_gives_the_expiry_the_constant_gives(label: str) -> None:
    """A pending confirmation lasts `CONFIRMATION_EXPIRY_HOURS` hours, and every copy that says so says that number.

    Mutations: change 24 to 12 in one copy; set the constant to 12 (every copy then fails).
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    hours = _expiry_hours(text)
    assert hours, f"{label} no longer says how long a pending confirmation lasts"
    assert set(hours) == {CONFIRMATION_EXPIRY_HOURS}, (label, hours, CONFIRMATION_EXPIRY_HOURS)


def test_the_protocol_names_the_constant_and_its_value() -> None:
    """The canonical section ties the number to `CONFIRMATION_EXPIRY_HOURS` and says where it lives.

    Mutation: change the number in ``lasts 24 hours`` or delete the constant name.
    """

    text = _doc_text("protocol")
    match = re.search(r"lasts (\d+) hours, the value of `CONFIRMATION_EXPIRY_HOURS` in `vnext_memory_commit.py`", text)
    assert match is not None
    assert int(match.group(1)) == CONFIRMATION_EXPIRY_HOURS


@pytest.mark.parametrize("label", ["protocol", "tool reference"])
def test_the_two_copies_that_state_the_refusal_agree_on_the_code_and_the_message(label: str) -> None:
    """What a refused stdio caller gets: `tool_request_failed` with the fixed message, and, under one marker, the typed codes.

    The marker is the one the other pages use, and the typed codes are main-only, so v0.20.0 readers see
    `tool_request_failed` for every refusal. A credential refusal stays `tool_request_failed`.

    Mutations: change ``not_permitted`` to ``not_found`` for the author and ceiling refusal in one copy; delete
    the marker; delete the fixed message; delete the sentence about the credential refusal.
    """

    text = _doc_text(label)
    assert (
        "Over the stdio server, a refused confirm or reject, and a credential refusal on confirm, comes back as "
        f"`tool_request_failed` with the message `{FIXED_MESSAGE}` and no reason code"
    ) in text, label
    stdio = text[text.index("Over the stdio server, a refused confirm or reject") :]
    stdio_before, stdio_marker, stdio_after = stdio.partition(MARK)
    assert stdio_marker
    assert "not_permitted" not in stdio_before and "not_found" not in stdio_before
    tail = stdio_after[:700]
    assert re.search(r"an author refusal and a ceiling refusal come back as `not_permitted`", tail), label
    assert "`not_found`" in tail and "`precondition_failed`" in tail, label
    assert re.search(r"credential refusal stays `tool_request_failed`", tail), label


# --- the copies are the rules the server applies ---------------------------------------------------------------


def _wire(ctx, name: str, arguments: dict) -> tuple[bool, dict]:
    """One ``tools/call`` through the real server: the error flag and the decoded body."""

    server = mcp_server.MCPServer(context=ctx, input_stream=BytesIO(), output_stream=BytesIO())
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = server._handle_request(request)
    assert response is not None
    result = response["result"]
    return bool(result["isError"]), json.loads(result["content"][0]["text"])


def _answer(ctx, confirmation_id: str, **identity: str) -> tuple[bool, dict]:
    return _wire(
        ctx,
        "alice_memory_commit",
        {"confirmation_id": confirmation_id, "confirmation_action": "confirm", **identity},
    )


def test_the_server_lets_exactly_the_documented_callers_answer(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a caller of each kind against a pending write and read who was let through.

    The author, an `admin_agent` key and the owner are let through. Another agent of the same profile and a
    read-only agent are not. The result is compared with the set every copy names, so the docs cannot all drift
    together.

    Mutation: in ``VNextMemoryCommitService.confirm``, skip the author check so any agent that passes the policy can
    answer. The other trusted agent is then let through and this test fails.
    """

    others = {
        "another trusted agent": {
            "agent_id": "intruder",
            "agent_type": "personal_assistant",
            "permission_profile": "trusted_local_agent",
        },
        "a read-only agent": {
            "agent_id": "reader",
            "agent_type": "coding_agent",
            "permission_profile": "read_only_agent",
        },
    }
    let_through: set[str] = set()
    refused: set[str] = set()

    for label, identity in {"author": AUTHOR, "owner": {}, **others}.items():
        ctx = _context(tmp_path / label.replace(" ", "-"))
        pending = _commit_pending(ctx, **AUTHOR)
        is_error, payload = _answer(ctx, str(pending["confirmation_id"]), **identity)
        (refused if is_error else let_through).add(label)
        if is_error:
            assert payload["error"]["message"] == FIXED_MESSAGE, payload

    ctx = _context(tmp_path / "admin-key")
    pending = _commit_pending(ctx, **AUTHOR)
    _mint_key(ctx, monkeypatch, agent_id="operator", permission_profile="admin_agent")
    is_error, _payload = _answer(ctx, str(pending["confirmation_id"]))
    (refused if is_error else let_through).add("admin_agent key")

    assert let_through == set(WHO_MAY_ANSWER), (sorted(let_through), sorted(refused))
    assert refused == set(others), sorted(refused)


def test_the_server_exempts_exactly_the_documented_callers_from_the_ceiling(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confidential write is held for confirmation for an exempt caller and rejected for an agent under its ceiling.

    The owner, a keyless call that declares `admin_agent` and an `admin_agent` key get `confirmation_required`;
    a keyless call that declares a trusted agent gets `rejected` with `sensitivity_above_agent_ceiling`. The set that
    was held for confirmation is the set every copy names.

    Mutation: in the policy engine, stop exempting a keyless call that declares `admin_agent`; this test fails.
    """

    confidential = {
        "title": "Salary band",
        "canonical_text": "The user's salary band is confidential.",
        "sensitivity": "confidential",
        "confidence": 0.95,
    }
    exempt: set[str] = set()
    held: dict[str, str] = {}
    callers = {
        "owner": {},
        "keyless call that declares admin_agent": {
            "agent_id": "operator",
            "agent_type": "workflow_agent",
            "permission_profile": "admin_agent",
        },
        "trusted agent": AUTHOR,
    }
    for label, identity in callers.items():
        ctx = _context(tmp_path / label.replace(" ", "-"))
        is_error, payload = _wire(ctx, "alice_memory_commit", {**confidential, **identity})
        assert not is_error, payload
        if payload["status"] == "confirmation_required":
            exempt.add(label)
        else:
            held[label] = str(payload.get("reason"))

    ctx = _context(tmp_path / "admin-key")
    _mint_key(ctx, monkeypatch, agent_id="operator-key", permission_profile="admin_agent")
    is_error, payload = _wire(ctx, "alice_memory_commit", dict(confidential))
    assert not is_error, payload
    if payload["status"] == "confirmation_required":
        exempt.add("admin_agent key")

    assert exempt == set(EXEMPT_FROM_CEILING), (sorted(exempt), held)
    assert held == {"trusted agent": "sensitivity_above_agent_ceiling"}, held


def test_the_server_answers_a_refusal_with_the_codes_the_two_copies_state(tmp_path: Path, default_surface) -> None:
    """Over stdio the author refusal is `not_permitted`, an unknown id `not_found`, an answered write `precondition_failed`.

    Each answer carries the one fixed message. These are the codes under the marker in the protocol page and the tool
    reference, so a code the docs name that the server does not send fails here.

    Mutation: in the typed error classes, send `tool_request_failed` for an author refusal; this test fails.
    """

    ctx = _context(tmp_path)
    pending = _commit_pending(ctx, **AUTHOR)
    intruder = {"agent_id": "intruder", "agent_type": "personal_assistant", "permission_profile": "trusted_local_agent"}

    def code(arguments: dict) -> str:
        is_error, payload = _wire(ctx, "alice_memory_commit", arguments)
        assert is_error, payload
        assert payload["error"]["message"] == FIXED_MESSAGE, payload
        return str(payload["error"]["code"])

    assert (
        code({"confirmation_id": pending["confirmation_id"], "confirmation_action": "confirm", **intruder})
        == "not_permitted"
    )
    assert code({"confirmation_id": "no-such-confirmation", "confirmation_action": "confirm", **AUTHOR}) == "not_found"
    answered = _wire(
        ctx,
        "alice_memory_commit",
        {"confirmation_id": pending["confirmation_id"], "confirmation_action": "confirm", **AUTHOR},
    )
    assert answered[0] is False, answered
    assert (
        code({"confirmation_id": pending["confirmation_id"], "confirmation_action": "confirm", **AUTHOR})
        == "precondition_failed"
    )


# --- no copy escapes the check ---------------------------------------------------------------------------------

# Dated records state what the rules were when a release was cut, and are never rewritten.
_RECORD_PREFIXES = (
    "docs/release/",
    "docs/handoff/",
    "docs/archive/",
    "docs/plans/",
    "docs/reports/",
    "docs/benchmarks/",
)


_SKIPPED_FOLDERS = {"node_modules", ".git", ".venv", "venv", "wiki", ".next", "dist", "build", "__pycache__"}


def test_no_other_living_file_states_who_may_confirm_or_the_ceiling_exemptions() -> None:
    """A file that says who may confirm or who is exempt must be one of the copies this test reads.

    The sweep reads every Markdown file outside the dated records, flattened, so a rule wrapped across lines is
    found. A new page that restates the rules fails here until it is added to ``DOCS`` and so held to the others.

    Mutation: add the sentence ``Only the author can confirm or reject a pending write.`` to any other page under
    ``docs``, or to ``README.md``.
    """

    registered = {path for path, _rules in DOCS.values()}
    stating = re.compile(r"can confirm or reject|not held to that ceiling|limited to its author")
    unregistered = []
    for folder, subfolders, names in os.walk(REPO_ROOT):
        subfolders[:] = [name for name in subfolders if name not in _SKIPPED_FOLDERS]
        for name in sorted(names):
            if not name.endswith(".md"):
                continue
            relative = (Path(folder) / name).relative_to(REPO_ROOT).as_posix()
            if relative in registered or relative == "CHANGELOG.md" or relative.startswith(_RECORD_PREFIXES):
                continue
            if stating.search(_flat((Path(folder) / name).read_text(encoding="utf-8"))):
                unregistered.append(relative)
    assert unregistered == [], f"states the confirm rules and is not read by this test: {sorted(unregistered)}"
