"""Parse JSONC in the install tests.

Comments and trailing commas are allowed. This module imports nothing
from alicebot_api, so a change here does not by itself schedule the
real-host job.
"""

from __future__ import annotations

import json


def _reject_constant(value: str) -> object:
    raise ValueError(value)


def _replace_comments(text: str) -> str:
    """Replace each comment with one space so adjacent tokens stay apart."""

    out: list[str] = []
    index = 0
    in_string = False
    escape = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            out.append(" ")
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            index = end + 2
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop a comma that follows a value and sits before ``}`` or ``]``.

    A comma with no value before it, as in ``{,}`` or ``[,]``, stays, so
    the JSON parser rejects it.
    """

    out: list[str] = []
    index = 0
    in_string = False
    escape = False
    last_value = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
                last_value = True
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if last_value and look < len(text) and text[look] in "}]":
                index += 1
                continue
            out.append(char)
            last_value = False
            index += 1
            continue
        out.append(char)
        if char not in " \t\r\n":
            last_value = char not in "{[:,"
        index += 1
    return "".join(out)


def parse(text: str) -> object:
    """``text`` as JSON after comments and trailing commas are removed."""

    return json.loads(
        _strip_trailing_commas(_replace_comments(text)),
        parse_constant=_reject_constant,
    )
