"""The Codex writer judge finds a bare LF in a CRLF file, and only there.

The judge skips its line-ending check when the original already has a bare
LF, so a false positive in this helper turns the check off for that file.
"""

from __future__ import annotations

import pytest

from tests.unit.toml_judge import has_bare_lf_outside_multiline


@pytest.mark.parametrize(
    "text",
    [
        "x = 1\r\n",
        "# it's here\r\nx = 1\r\n",
        '# say "hi\r\nx = 1\r\n',
        "x = 1 # don't\r\ny = 2\r\n",
        'x = "a # b"\r\n',
        'x = """\nkept\n"""\r\n',
        "x = '''\nkept\n'''\r\n",
        "# only a comment",
        "",
    ],
)
def test_crlf_text_has_no_bare_lf(text: str) -> None:
    """A quote in a comment, or a LF inside a multi-line string, is not a bare LF.

    Mutation: test quotes before ``#`` in ``has_bare_lf_outside_multiline``.
    The two comment cases read the comment's quote as a string opener and
    report a bare LF. This test fails.
    """

    assert not has_bare_lf_outside_multiline(text)


@pytest.mark.parametrize(
    "text",
    [
        "x = 1\ny = 2\r\n",
        "# it's here\r\nx = 1\n",
        "# it's here\nx = 1\r\n",
        '# say "hi\nx = 1\r\n',
        'x = "a # b"\ny = 1\r\n',
        'x = "a" # note\ny = 1\r\n',
        'x = """\nkept\n"""\ny = 1\r\n',
    ],
)
def test_bare_lf_outside_a_string_is_found(text: str) -> None:
    """A comment does not hide a bare LF after it, and a string does not hide one outside it."""

    assert has_bare_lf_outside_multiline(text)
