"""Independent judge for the Codex config writer.

This module imports nothing from alicebot_api. It checks an edit the writer
already produced: a refusal is allowed only for a mutant or a refusable
label, and a written file keeps every other byte except at most two
alice spans.
"""

from __future__ import annotations

import tomllib
from typing import Mapping

REFUSABLE_LABELS = frozenset({"comment-in-args"})


class JudgeFailure(AssertionError):
    """The edit changed something outside Alice or refused a required case."""


def _newline(text: str) -> str:
    if "\r\n" in text and "\n" in text.replace("\r\n", ""):
        return "\n"
    return "\r\n" if "\r\n" in text else "\n"


def _skip_string(text: str, index: int, nl: str) -> int:
    if text.startswith('"""', index) or text.startswith("'''", index):
        quote = text[index]
        index += 3
        while index < len(text):
            if text.startswith(quote * 3, index):
                run = index
                while run < len(text) and text[run] == quote:
                    run += 1
                count = run - index
                if count >= 3:
                    return index + min(count, 5)
            if quote == '"' and text[index] == "\\":
                index += 2
                continue
            index += 1
        return index
    quote = text[index]
    index += 1
    while index < len(text):
        if quote == '"' and text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        if text.startswith(nl, index):
            return index
        index += 1
    return index


def comment_tokens(text: str) -> list[str]:
    """Stamp tokens on real comments, not on a ``#`` inside a string."""

    nl = _newline(text)
    found: list[str] = []
    index = 0
    while index < len(text):
        if text[index] in "\"'":
            index = _skip_string(text, index, nl)
            continue
        if text[index] == "#":
            end = index
            while end < len(text) and not text.startswith(nl, end):
                end += 1
            comment = text[index:end]
            token_at = comment.find(" c")
            if token_at >= 0:
                token = comment[token_at + 2 : token_at + 8]
                if len(token) == 6 and all(char in "0123456789abcdef" for char in token):
                    found.append(token)
            index = end
            continue
        index += 1
    return found


def _strip(doc: Mapping[str, object]) -> dict[str, object]:
    copied = _clone(doc)
    servers = copied.get("mcp_servers")
    if isinstance(servers, dict) and isinstance(servers.get("alice"), dict):
        alice = servers["alice"]
        for key in ("command", "args", "env"):
            alice.pop(key, None)
        if not alice:
            servers.pop("alice")
        if not servers:
            copied.pop("mcp_servers", None)
    return copied


def _clone(value: object) -> object:
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


def _alice_only(text: str) -> bool:
    if text.strip() == "":
        return True
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    if not isinstance(loaded, dict):
        return False
    extra = [key for key in loaded if key != "mcp_servers"]
    if extra:
        return False
    servers = loaded.get("mcp_servers")
    if servers is None:
        return True
    if not isinstance(servers, dict):
        return False
    return set(servers) <= {"alice"}


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    index = 0
    while index < len(text):
        newline = text.find("\n", index)
        if newline < 0:
            offsets.append(len(text))
            break
        offsets.append(newline + 1)
        index = newline + 1
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def _one_span(original: str, output: str) -> tuple[str, str] | None:
    """A line-bounded replacement that parses as alice keys only, if one exists."""

    for start in _line_offsets(original):
        for end in _line_offsets(original):
            if end < start:
                continue
            prefix = original[:start]
            suffix = original[end:]
            if len(prefix) + len(suffix) > len(output):
                continue
            if not output.startswith(prefix) or (suffix and not output.endswith(suffix)):
                continue
            inserted = output[len(prefix) : len(output) - len(suffix)]
            if prefix + inserted + suffix != output:
                continue
            removed = original[start:end]
            if _alice_only(removed) and _alice_only(inserted):
                return removed, inserted
    return None


def _spans(original: str, output: str) -> list[tuple[str, str]]:
    """At most two changed spans, as (removed, inserted) pairs."""

    if original == output:
        return []
    one = _one_span(original, output)
    if one is not None:
        return [one]
    # Two spans: one shared middle that is unchanged, each side one alice span.
    for mark in _line_offsets(original):
        for stop in _line_offsets(original):
            if stop <= mark:
                continue
            middle = original[mark:stop]
            if middle == "":
                continue
            at = output.find(middle)
            if at < 0:
                continue
            left = _one_span(original[:mark], output[:at])
            right = _one_span(original[stop:], output[at + len(middle) :])
            if left is None or right is None:
                continue
            return [left, right]
    raise JudgeFailure("the edit is more than two alice spans")


def judge_case(
    *,
    original: str,
    output: str | None,
    refused: bool,
    label: str,
    mutant: bool,
) -> None:
    """Raise JudgeFailure when a case breaks the writer's contract."""

    if refused:
        if not mutant and label not in REFUSABLE_LABELS:
            raise JudgeFailure(f"refused a {label} config")
        return
    if output is None:
        output = original
    try:
        before = tomllib.loads(original) if original.strip() else {}
        after = tomllib.loads(output) if output.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise JudgeFailure(f"output does not parse: {exc}") from exc
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise JudgeFailure("output is not a table")
    old_tokens = comment_tokens(original)
    new_tokens = comment_tokens(output)
    if old_tokens != new_tokens:
        raise JudgeFailure(f"stamped comments changed: {old_tokens} -> {new_tokens}")
    _spans(original, output)
    if _strip(before) != _strip(after):
        raise JudgeFailure("a value outside alice changed")
