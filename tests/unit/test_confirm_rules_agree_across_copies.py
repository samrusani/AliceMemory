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
  `permission_profile: admin_agent` (the `sensitivity` property of `alice_memory_commit` names the first two and
  may leave out the third, but it may not name anyone else); and that the author can still reject its own pending
  write above the ceiling;
* what an agent write above the ceiling gets: it is rejected and not saved, the agent is told not to retry with a
  lower label and to tell the user, and the owner can raise the clearance or store the memory; and the level the
  ceiling sits at, derived from the server;
* the expiry in hours, read from `CONFIRMATION_EXPIRY_HOURS`, never typed here;
* what a refused stdio caller gets: the code and the fixed message, and the main-only codes under their marker.

The sets of who may answer and who is exempt, and the level of the ceiling, are also derived from the server by
running real callers, so a copy that agrees with the others and disagrees with the code fails too. A last check
finds any other tracked Markdown file outside the dated records that states these rules and asks for it to be
registered here.

Mutations, each one alone (every one fails a test below):

* in `docs/alpha/mcp-tools.md`, change `an `admin_agent` key, or the owner can confirm` to `a `trusted_local_agent`
  key, or the owner can confirm`; delete `the owner` from the exemption sentence of `docs/alpha/agent-integration.md`;
* in `agent-skills/openclaw/alice-project-memory/SKILL.md`, change `After 24 hours` to `After 12 hours`; in
  `docs/alpha/hermes-skill.md`, change `After 24 hours` to `After 48 hours`;
* in `apps/api/src/alicebot_api/mcp/definitions.py`, change `After 24 hours a pending write` in the
  `alice_memory_commit` description to `After 12 hours a pending write`, or the `sensitivity` property's `The owner
  and an admin key still confirm levels above private` to `Only the owner still confirms levels above private`;
* set `CONFIRMATION_EXPIRY_HOURS` to 12 in `vnext_memory_commit.py` (the docs then disagree with it);
* in the protocol page, change `not_permitted` to `not_found` in the stdio sentence, or drop the keyless caveat
  from any one copy;
* in `vnext_memory_commit.py`, let any caller confirm (the server then disagrees with every doc), or let a keyless
  call that declares `admin_agent` answer (drop the `auth == "agent_api_key"` condition of
  `caller_may_resolve_pending_write`).
"""

from __future__ import annotations

import json
import re
import subprocess
from io import BytesIO
from pathlib import Path

import pytest

from alicebot_api import mcp_server
from alicebot_api.vnext_memory_commit import CONFIRMATION_EXPIRY_HOURS, VNEXT_SENSITIVITY_LEVELS
from tests.unit.test_default_surface_can_finish_confirmation_required import (  # noqa: F401  (fixture)
    _commit_pending,
    _context,
    _mint_key,
    default_surface,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every file that states the rules, and the rules each one must state. A rule a copy does not need to state is not
# listed for it: the skill packs tell an agent what to do and leave out the exemptions an agent cannot use.
#
# WHO: who may confirm or reject. KEYLESS: on a keyless install the author limit is not protection. EXEMPT: the three
# callers not held to the ceiling. EXEMPT_PARTIAL: the owner and an admin key, with nobody outside the three (a short
# statement may leave out the keyless call that declares `admin_agent`). REJECT_ABOVE: the author can still reject its
# own pending write above the ceiling. CEILING_REJECT: an agent write above the ceiling is rejected and not saved.
# NO_RETRY: the agent is told not to retry with a lower label, and to tell the user. OWNER_REMEDY: the owner can raise
# the clearance or store the memory. CEILING_LEVEL: the level the ceiling sits at. EXPIRY: the hours a pending
# confirmation lasts. STDIO: what a refused stdio caller gets.
WHO, KEYLESS, EXEMPT, EXEMPT_PARTIAL, REJECT_ABOVE = "who", "keyless", "exempt", "exempt_partial", "reject_above"
CEILING_REJECT, NO_RETRY, OWNER_REMEDY, CEILING_LEVEL = "ceiling_reject", "no_retry", "owner_remedy", "ceiling_level"
EXPIRY, STDIO = "expiry", "stdio"
_CEILING_RULES = frozenset({CEILING_REJECT, NO_RETRY, OWNER_REMEDY})

DOCS: dict[str, tuple[str, frozenset[str]]] = {
    "protocol": (
        "docs/memory-operations-protocol.md",
        frozenset({WHO, KEYLESS, EXEMPT, REJECT_ABOVE, EXPIRY, STDIO}) | _CEILING_RULES,
    ),
    "tool reference": (
        "docs/alpha/mcp-tools.md",
        frozenset({WHO, KEYLESS, EXEMPT, EXPIRY, STDIO, CEILING_REJECT}),
    ),
    "integration guide": (
        "docs/alpha/agent-integration.md",
        frozenset({WHO, KEYLESS, EXEMPT, EXPIRY, CEILING_REJECT, NO_RETRY}),
    ),
    "hermes skill pack": (
        "agent-skills/hermes/alice-memory/SKILL.md",
        frozenset({WHO, KEYLESS, REJECT_ABOVE, EXPIRY, CEILING_LEVEL}) | _CEILING_RULES,
    ),
    "openclaw skill pack": (
        "agent-skills/openclaw/alice-project-memory/SKILL.md",
        frozenset({WHO, KEYLESS, EXPIRY}) | _CEILING_RULES,
    ),
    "hermes skill page": (
        "docs/alpha/hermes-skill.md",
        frozenset({WHO, KEYLESS, REJECT_ABOVE, EXPIRY, CEILING_LEVEL}) | _CEILING_RULES,
    ),
    "openclaw skill page": (
        "docs/alpha/openclaw-skill.md",
        frozenset({WHO, KEYLESS, EXPIRY}) | _CEILING_RULES,
    ),
}
# What an agent reads in `tools/list`: (tool name, property or None for the tool description) and the rules it states.
# Every description and property of `alice_memory_commit` and `alice_memory_manage` that states one of the rules is
# listed, so a rule cannot drift in the registry while the docs stay put.
TOOL_COPIES: dict[str, tuple[str, str | None, frozenset[str]]] = {
    "alice_memory_commit description": (
        "alice_memory_commit",
        None,
        frozenset({EXPIRY}) | _CEILING_RULES,
    ),
    "alice_memory_commit sensitivity": (
        "alice_memory_commit",
        "sensitivity",
        frozenset({CEILING_REJECT, EXEMPT_PARTIAL, CEILING_LEVEL}),
    ),
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


def _partial_exemptions(text: str) -> list[frozenset[str]]:
    """Every short sentence that says who still confirms a level above the ceiling, as the set of callers it names.

    The `sensitivity` property says `The owner and an admin key still confirm levels above private`. It names two of
    the three callers and may leave out the keyless call that declares `admin_agent`. It may not name a fourth.
    """

    said: list[frozenset[str]] = []
    for match in re.finditer(r"(?P<who>[Tt]he owner[^.]*?)\s+still confirm levels above", text):
        fragment = match.group("who")
        named: set[str] = set()
        if re.search(r"\bowner\b", fragment):
            named.add("owner")
        if re.search(r"\badmin(?:_agent)?`? key\b", fragment):
            named.add("admin_agent key")
        named.update(profile for profile in OTHER_PROFILES if profile in fragment)
        said.append(frozenset(named))
    return said


# What an agent write above the ceiling gets. A statement is a write (or a commit) "above it" or above the ceiling, or
# the short form of the `sensitivity` property; what follows must say it is rejected. A pending write above the ceiling
# is the author's reject, which is another rule.
_CEILING_WRITE = re.compile(r"(?<!pending )\b(?:write|commit)\b[^.]{0,80}?\babove (?:it|[^.]{0,60}?\bceiling)\b")
_CEILING_SHORT = re.compile(r"\bAbove the caller's ceiling the commit\b")
_IS_REJECTED = re.compile(r"[^.]{0,80}?\bis rejected\b")
_NOT_SAVED = re.compile(r"not saved|nothing is saved|no pending row")
_NO_RETRY = re.compile(r"[Dd]o not retry (?:it )?with a lower sensitivity label[.,] ?[Tt]ell the user")
_OWNER_REMEDY = re.compile(r"[Tt]he owner can raise this agent's clearance or store the memory themselves")
_CEILING_LEVEL = re.compile(
    r"anything above `?(?P<a>[a-z_]+)`? for `?trusted_local_agent`?|\blevels above (?P<b>[a-z_]+)\b"
)


def _ceiling_levels(text: str) -> list[str]:
    return [m.group("a") or m.group("b") for m in _CEILING_LEVEL.finditer(text)]


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


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if EXEMPT_PARTIAL in rules])
def test_a_short_exemption_names_the_owner_and_an_admin_key_and_nobody_else(label: str) -> None:
    """The `sensitivity` property of `alice_memory_commit` names the owner and an admin key, and no other caller.

    It may leave out the keyless call that declares `admin_agent`, because the property is one line of a schema;
    the full list is in the copies that carry EXEMPT.

    Mutations: change ``The owner and an admin key still confirm`` to ``Only the owner still confirms`` or to
    ``The owner and a trusted agent still confirm``; delete the sentence.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    said = _partial_exemptions(text)
    assert said, f"{label} no longer says who still confirms a level above the ceiling"
    for named in said:
        assert {"owner", "admin_agent key"} <= named <= EXEMPT_FROM_CEILING, (label, sorted(named))


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if CEILING_REJECT in rules])
def test_every_copy_says_an_agent_write_above_the_ceiling_is_rejected_and_not_saved(label: str) -> None:
    """A write above the agent's ceiling is rejected, and nothing is saved, in every copy that says what it gets.

    Every statement of what a write above the ceiling gets must say it is rejected, and at least one in the copy says
    nothing is saved.

    Mutations: say it is ``held for confirmation`` instead of ``rejected`` in one copy; change ``This was not saved``
    to ``This was saved``; delete the sentence from one copy.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    tails = [text[m.end() : m.end() + 160] for m in (*_CEILING_WRITE.finditer(text), *_CEILING_SHORT.finditer(text))]
    assert tails, f"{label} no longer says what a write above the ceiling gets"
    for tail in tails:
        assert _IS_REJECTED.match(tail), (label, tail)
    after = [tail[m.end() : m.end() + 60] for tail in tails if (m := _IS_REJECTED.match(tail))]
    assert any(_NOT_SAVED.search(words) for words in after), (label, after)


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if NO_RETRY in rules])
def test_every_copy_tells_the_agent_not_to_retry_with_a_lower_label_and_to_tell_the_user(label: str) -> None:
    """After a ceiling rejection the agent does not retry with a lower sensitivity label, and tells the user.

    Mutations: change ``Do not retry`` to ``Retry`` in one copy; delete ``Tell the user.`` from one copy.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    assert _NO_RETRY.search(text), label


@pytest.mark.parametrize("label", [label for label, _text, rules in _copies() if OWNER_REMEDY in rules])
def test_every_copy_that_names_the_remedy_says_the_owner_can_raise_the_clearance_or_store_it(label: str) -> None:
    """The owner can raise this agent's clearance or store the memory themselves, in every copy that gives a remedy.

    Mutations: change ``the owner can raise`` to ``the agent can raise`` in one copy; delete the sentence.
    """

    text = dict((l, t) for l, t, _r in _copies())[label]
    assert _OWNER_REMEDY.search(text), label


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

    The author, an `admin_agent` key and the owner are let through. Another agent of the same profile, a read-only
    agent and a keyless call that only declares `permission_profile: admin_agent` are not: the docs say an
    `admin_agent` key, and a declared profile is not a key. The result is compared with the set every copy names, so
    the docs cannot all drift together.

    Mutations, each one alone, in ``vnext_memory_commit.py``: skip the author check in
    ``caller_may_resolve_pending_write`` so any agent that passes the policy can answer (the other trusted agent is
    let through); drop the ``identity.auth == "agent_api_key"`` condition so a declared admin profile can answer
    (the keyless declared admin is let through).
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
        "a keyless call that declares admin_agent": {
            "agent_id": "operator",
            "agent_type": "workflow_agent",
            "permission_profile": "admin_agent",
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


def test_the_ceiling_level_every_copy_gives_is_the_one_the_server_applies(
    tmp_path: Path, default_surface
) -> None:
    """Commit at each level as a trusted agent and read where the ceiling sits; every copy that names it agrees.

    The levels the server lets through form a prefix of the list, and the last of them is the ceiling. The skill pages
    and the `sensitivity` property name that level, so a change of the ceiling that leaves a copy behind fails here.

    Mutation: change ``anything above `private` for `trusted_local_agent` `` to ``anything above `internal` for
    `trusted_local_agent` `` in the Hermes skill page, or ``levels above private`` to ``levels above internal`` in
    the `sensitivity` property; or give the trusted agent a different ceiling in the policy, which moves the derived
    level and fails every copy.
    """

    levels = [level for level in VNEXT_SENSITIVITY_LEVELS if level != "unknown"]
    let_through: list[str] = []
    for level in levels:
        ctx = _context(tmp_path / level)
        is_error, payload = _wire(
            ctx,
            "alice_memory_commit",
            {
                "title": f"Probe at {level}",
                "canonical_text": f"A probe written at the {level} level.",
                "sensitivity": level,
                "confidence": 0.95,
                **AUTHOR,
            },
        )
        assert not is_error, payload
        if not (payload["status"] == "rejected" and payload.get("reason") == "sensitivity_above_agent_ceiling"):
            let_through.append(level)
    assert let_through == levels[: len(let_through)] and let_through, let_through
    ceiling = let_through[-1]

    named = [(label, _ceiling_levels(text)) for label, text, rules in _copies() if CEILING_LEVEL in rules]
    assert named, "no copy names the level the ceiling sits at"
    for label, found in named:
        assert found, f"{label} no longer says where the ceiling sits"
        assert set(found) == {ceiling}, (label, found, ceiling)


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

# A sentence that states a rule for answering a pending write: who may confirm or reject (in any of the ways it can be
# put), who the author is, who is exempt from the ceiling, the constant or the reason code of an expiry, or the hours a
# pending write lasts. Each alternative matches nothing on any tracked page outside the copies today.
STATES_THE_RULES = re.compile(
    r"can confirm or reject|not held to (?:that|the) ceiling|limited to its author"
    r"|\b(?:can|may|could|must|only)\b[^.]{0,60}\b(?:confirm|reject)(?:s|ed)?\b[^.]{0,40}\b(?:pending|confirmation)"
    r"|\bauthor\b[^.]{0,80}\b(?:confirm|reject|answer|resolve)"
    r"|\bexempt(?:ed)?\b[^.]{0,40}\bceiling"
    r"|\bpending\b[^.]{0,60}\b(?:confirmed|rejected|answered) by\b"
    r"|CONFIRMATION_EXPIRY_HOURS|confirmation_expired"
    r"|\b\d+[ -]hours?\b[^.]{0,80}\b(?:pending|confirm)|\b(?:pending|confirm)[^.]{0,80}\b\d+[ -]hours?\b"
)


def _tracked_markdown_paths() -> list[str]:
    """The Markdown files git tracks, as paths from the repository root.

    A walk of the working tree would read an untracked note, a nested worktree or an editor folder, so the test could
    fail on one machine and pass in CI. Tracked files are what ships.
    """

    listed = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.md"), capture_output=True, check=False
    )
    assert listed.returncode == 0, listed.stderr.decode("utf-8", errors="replace")
    return sorted(item.decode("utf-8") for item in listed.stdout.split(b"\0") if item)


@pytest.mark.parametrize(
    "sentence",
    [
        "Only the author can confirm or reject a pending write.",
        "Only the author may confirm a pending write.",
        "A pending write may be confirmed by its author or by the owner.",
        "Only the owner may confirm a pending write above the ceiling.",
        "The author of a pending write can reject it.",
        "An admin_agent key is exempt from the sensitivity ceiling.",
        "The owner and an admin key are not held to the ceiling.",
        "Confirm and reject are limited to its author, an admin key or the owner.",
        "A pending write expires after 24 hours.",
        "A pending confirmation lasts 12 hours.",
        "After 48 hours the confirm is refused.",
        "The expiry is CONFIRMATION_EXPIRY_HOURS in the commit service.",
        "A late confirm resolves to rejected with reason confirmation_expired.",
    ],
)
def test_the_sweep_recognises_each_wording_of_the_rules(sentence: str) -> None:
    """The pattern that finds a page stating the rules matches the ways the rules are put, not three phrasings.

    Mutation: narrow ``STATES_THE_RULES`` to the three phrases it used to hold (``can confirm or reject``,
    ``not held to that ceiling``, ``limited to its author``); every sentence here that does not use one of those
    phrases then fails.
    """

    assert STATES_THE_RULES.search(_flat(sentence)), sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "Run the confirm step after the import, then check the result.",
        "Reject the change if the test fails.",
        "The author of the commit is recorded in the audit trail.",
        "The token lasts 24 hours.",
        "A sensitivity ceiling holds a profile to a level.",
        "Reviewers confirm or reject memories in the console.",
    ],
)
def test_the_sweep_does_not_match_a_sentence_that_states_no_rule(sentence: str) -> None:
    """Ordinary sentences with the same words do not fail the sweep.

    Mutation: widen ``STATES_THE_RULES`` to match ``confirm`` or ``ceiling`` alone; these sentences then match.
    """

    assert not STATES_THE_RULES.search(_flat(sentence)), sentence


def test_no_other_living_file_states_who_may_confirm_or_the_ceiling_exemptions() -> None:
    """A tracked Markdown file that states the rules must be one of the copies this test reads.

    The sweep reads every Markdown file git tracks outside the dated records, flattened, so a rule wrapped across
    lines is found. A new page that restates the rules fails here until it is added to ``DOCS`` and so held to the
    others.

    Mutation: add the sentence ``Only the author can confirm or reject a pending write.`` or ``Only the author may
    confirm a pending write.`` to any other tracked page under ``docs``, or to ``README.md``, and run ``git add`` on it.
    """

    registered = {path for path, _rules in DOCS.values()}
    paths = _tracked_markdown_paths()
    assert len(paths) >= 100, "the list of tracked Markdown files is empty or short"
    unregistered = []
    for relative in paths:
        if relative in registered or relative == "CHANGELOG.md" or relative.startswith(_RECORD_PREFIXES):
            continue
        path = REPO_ROOT / relative
        if path.is_file() and STATES_THE_RULES.search(_flat(path.read_text(encoding="utf-8"))):
            unregistered.append(relative)
    assert unregistered == [], f"states the confirm rules and is not read by this test: {sorted(unregistered)}"
