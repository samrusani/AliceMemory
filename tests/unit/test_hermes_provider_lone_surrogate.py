"""The Hermes provider sends text the server accepts, so a lone surrogate does not cost the turn.

Plugin 0.5.2 stopped raising ``UnicodeEncodeError`` out of ``sync_turn`` for a
turn whose text held a lone surrogate, but it still sent the surrogate, and the
server refused that request body, so the turn was lost. Plugin 0.5.3 replaces
each lone surrogate with U+FFFD before it queues, fingerprints or sends the
text, so the turn is saved with one replacement character where the surrogate
was, and records nothing else about it.

The tests drive the real provider. ``_request_json`` is routed to the real
capture functions over the in-memory continuity store, as in
``test_hermes_provider_turn_roles``, and every request body is also checked with
the server's own detector.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest

from alicebot_api.lone_surrogates import body_lone_surrogate_location
from tests.unit.test_continuity_capture import ContinuityCaptureStoreStub
from tests.unit.test_hermes_memory_provider import PROVIDER_PATH, _load_provider_module
from tests.unit.test_hermes_provider_turn_roles import (
    _CANDIDATES_PATH,
    _COMMIT_PATH,
    _RAW_PATH,
    _active,
    _flush,
    _posted,
    _provider,
)

REPLACEMENT = chr(0xFFFD)
EMOJI = chr(0x1F600)
# Python joins the two escapes of a pair in a string literal, so the two separate
# code points of an unjoined pair are built with chr().
HIGH = chr(0xD83D)
LOW = chr(0xDE00)


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def _assert_every_body_is_accepted_by_the_server(posts: list[tuple[str, dict[str, Any]]]) -> None:
    """Each payload, JSON-encoded as the plugin encodes it, passes the server's own check."""

    assert posts
    for path, payload in posts:
        body = json.dumps(payload).encode("utf-8")
        assert body_lone_surrogate_location(body) is None, path


@pytest.mark.parametrize(
    ("user", "reply", "expected_user", "expected_reply"),
    (
        ("before \ud800 after", "fine", f"before {REPLACEMENT} after", "fine"),
        ("fine", "before \udc00 after", "fine", f"before {REPLACEMENT} after"),
        ("trailing high \ud83d", "leading low \ude00 end", f"trailing high {REPLACEMENT}", f"leading low {REPLACEMENT} end"),
        ("\udfff\ud800", "\ud800\ud800", f"{REPLACEMENT}{REPLACEMENT}", f"{REPLACEMENT}{REPLACEMENT}"),
    ),
    ids=("user-high", "assistant-low", "both-sides", "adjacent-lone-ones"),
)
def test_a_turn_with_a_lone_surrogate_is_saved_with_a_replacement_character(
    monkeypatch: pytest.MonkeyPatch,
    user: str,
    reply: str,
    expected_user: str,
    expected_reply: str,
) -> None:
    """The text that is sent carries U+FFFD where the surrogate was, on each side.

    Mutation: remove ``_without_lone_surrogates`` from ``sync_turn``. The
    surrogate is sent as it is, the server's detector finds it, and the posted
    text no longer equals the expected text.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts)

    provider.sync_turn(user, reply)
    _flush(provider)

    candidate_posts = _posted(posts, _CANDIDATES_PATH)
    assert len(candidate_posts) == 1
    assert candidate_posts[0]["user_content"] == expected_user
    assert candidate_posts[0]["assistant_content"] == expected_reply
    assert len(_posted(posts, _COMMIT_PATH)) == 1
    _assert_every_body_is_accepted_by_the_server(posts)


def test_an_explicit_user_decision_that_carries_a_surrogate_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn is saved, not only sent: the stored decision holds the replacement character.

    Mutation: remove the replacement from ``sync_turn``. The server's capture
    code takes the surrogate in as it is and the stored title carries it, so the
    title assertion fails.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts, mode="auto")

    provider.sync_turn("Decision: ship the \ud800 build on Friday", "Noted.")
    _flush(provider)

    titles = [str(row["title"]) for row in _active(store)]
    assert titles == [f"Decision: ship the {REPLACEMENT} build on Friday"]
    assert not any(_has_surrogate(title) for title in titles)


def test_a_valid_pair_kept_as_two_characters_becomes_the_one_character_it_stands_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high surrogate directly followed by a low one is joined, not replaced.

    The server's JSON decoder joins the two escapes the plugin would write, so
    this is the text the turn had before. Text with no surrogate is untouched.
    Mutation: replace each surrogate on its own (``re.sub`` to U+FFFD). The emoji
    turns into two replacement characters.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts)
    plain = f"caf{chr(0xE9)} {chr(0x65E5)}{chr(0x672C)}{chr(0x8A9E)} {EMOJI} and {chr(0xFC)}ber"
    unjoined = f"smile {HIGH}{LOW} here"
    assert len(unjoined) == len("smile  here") + 2

    provider.sync_turn(unjoined, plain)
    _flush(provider)

    (candidate,) = _posted(posts, _CANDIDATES_PATH)
    assert candidate["user_content"] == f"smile {EMOJI} here"
    assert candidate["assistant_content"] == plain
    _assert_every_body_is_accepted_by_the_server(posts)


def test_the_replacement_happens_after_the_cut_that_can_split_a_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair that straddles the character limit leaves a lone high surrogate, which is replaced.

    Mutation: replace before the cut instead of after. The pair is joined, the
    emoji is kept, and the text is not the expected one.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts)
    limit = provider.sync_turn.__func__.__globals__["_DEFAULT_CAPTURE_CHAR_LIMIT"]  # type: ignore[attr-defined]

    provider.sync_turn("u" * (limit - 1) + HIGH + LOW + " tail", "reply")
    _flush(provider)

    (candidate,) = _posted(posts, _CANDIDATES_PATH)
    assert candidate["user_content"] == "u" * (limit - 1) + REPLACEMENT
    _assert_every_body_is_accepted_by_the_server(posts)


def test_a_turn_before_and_after_replacement_is_one_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fingerprint is of the replaced text, so the two spellings dedupe.

    Mutation: replace inside the send path (``_post_sync_turn_capture``) and
    fingerprint the original. The two spellings then queue as two captures.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts)

    provider.sync_turn("same \ud800 text", "reply")
    provider.sync_turn(f"same {REPLACEMENT} text", "reply")
    _flush(provider)

    assert len(_posted(posts, _CANDIDATES_PATH)) == 1
    assert len(_posted(posts, _COMMIT_PATH)) == 1


def test_nothing_else_is_recorded_about_the_replacement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The plugin logs nothing, counts nothing and adds no field for a replaced surrogate.

    The same turn without a surrogate and with one leaves the same log
    messages, the same provider state and the same request fields. Mutations:
    add a ``logger.debug`` when the text changes; keep a replacement counter on
    the provider; add a field to the candidates payload when the text holds
    U+FFFD.
    """

    def run(user: str) -> tuple[list[str], set[str], list[set[str]], int]:
        store, posts = ContinuityCaptureStoreStub(), []
        provider = _provider(monkeypatch, store, posts)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            provider.sync_turn(user, "reply")
            _flush(provider)
        messages = [record.getMessage() for record in caplog.records]
        fields = [set(payload) for _path, payload in posts]
        return messages, set(vars(provider)), fields, provider._capture_dropped_count

    clean = run("text here")
    surrogate = run("text \ud800 here")

    assert surrogate == clean
    assert surrogate[3] == 0
    assert not any("surrogate" in message.lower() for message in surrogate[0])


@pytest.mark.parametrize("separator", ("", " "))
def test_the_memory_write_mirror_replaces_a_lone_surrogate_and_no_longer_raises(
    monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    """``on_memory_write`` raised ``UnicodeEncodeError`` in 0.5.2 for such text, out of Hermes' own write.

    It fingerprinted the str with ``.encode("utf-8")``. Mutation: remove the
    replacement from ``on_memory_write``. The call raises.
    """

    store, posts = ContinuityCaptureStoreStub(), []
    provider = _provider(monkeypatch, store, posts)
    provider._config["memory_write_capture_enabled"] = True

    provider.on_memory_write("add", "memory", f"prefers{separator}\ud800{separator}tabs")
    _flush(provider)

    (capture,) = _posted(posts, _RAW_PATH)
    assert capture["raw_content"] == f"Hermes built-in memory update (memory): prefers{separator}{REPLACEMENT}{separator}tabs"
    _assert_every_body_is_accepted_by_the_server(posts)


def test_the_prefetch_query_is_replaced_before_it_is_put_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``prefetch`` raised ``UnicodeEncodeError`` building the URL for a user message with a lone surrogate.

    It runs on Hermes' own thread. This drives the real ``_request_json`` with a
    fake ``urlopen``. Mutation: remove the replacement from
    ``_build_prefetch_context``. ``urlencode`` raises.
    """

    module = _load_provider_module(monkeypatch)
    provider = module.AliceMemoryProvider()
    provider._config = {
        "base_url": "http://127.0.0.1:8000",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "timeout_seconds": 5.0,
    }
    urls: list[str] = []

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"brief": {}}'

    def fake_urlopen(request: Any, timeout: float = 0) -> _Response:
        urls.append(request.full_url)
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert provider.prefetch("find \ud800 notes") == ""

    (url,) = urls
    assert "query=find+%EF%BF%BD+notes" in url


def test_text_without_a_surrogate_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replacement function is the identity for any text a surrogate is not in.

    Mutation: normalise the text (``NFC``, ``strip``) or round-trip it through a
    codec with ``errors="replace"``. A string with a non-BMP character, a
    combining mark or a NUL then changes.
    """

    module = _load_provider_module(monkeypatch)

    for text in ("", "plain", "caf" + chr(0xE9), "e" + chr(0x301) + " combining", "emoji " + EMOJI, "a" + chr(0) + "b", "  padded  ", REPLACEMENT + " already"):
        assert module._without_lone_surrogates(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("\ud800", REPLACEMENT),
        ("\udc00", REPLACEMENT),
        ("a\ud800b", f"a{REPLACEMENT}b"),
        ("ab\ud83d", f"ab{REPLACEMENT}"),
        ("\ude00ab", f"{REPLACEMENT}ab"),
        ("\ude00\ud83d", f"{REPLACEMENT}{REPLACEMENT}"),
        ("\ud800\ud800", f"{REPLACEMENT}{REPLACEMENT}"),
        ("\udc00\udc00", f"{REPLACEMENT}{REPLACEMENT}"),
        (f"{HIGH}{LOW}", EMOJI),
        (chr(0xD800) + HIGH + LOW, REPLACEMENT + EMOJI),
        (f"x{HIGH}{LOW}y" + chr(0xD800) + "z" + chr(0xDC00), f"x{EMOJI}y{REPLACEMENT}z{REPLACEMENT}"),
    ),
)
def test_every_arrangement_of_surrogates_ends_with_valid_text(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    """One replacement character per lone surrogate, a joined pair where there is one, and valid UTF-8.

    Mutation: replace only a high surrogate, or only a low one, or leave an
    unpaired surrogate at the end of the text.
    """

    module = _load_provider_module(monkeypatch)

    result = module._without_lone_surrogates(text)

    assert result == expected
    result.encode("utf-8")


def test_plugin_yaml_names_the_version_that_replaces_surrogates() -> None:
    """The provider is copied into Hermes, so its version is how an operator tells a fixed copy.

    0.5.2 shipped in v0.19.2 and does not replace a surrogate. Mutation: leave
    ``plugin.yaml`` at 0.5.2.
    """

    text = (PROVIDER_PATH.parent / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\d+)\.(\d+)\.(\d+)\s*$", text, flags=re.MULTILINE)
    assert match is not None
    assert tuple(int(part) for part in match.groups()) >= (0, 5, 3)
