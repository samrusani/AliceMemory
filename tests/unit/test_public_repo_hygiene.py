"""The repository is public: no personal details or local machine paths in it.

Until 2026-09-23 the scale benchmark wrote the absolute path of the machine it
ran on into the published results, and the owner's real name, company and email
were used as example data in the entity extractor's code and tests. Nothing
checked for either, because the secrets scanner only looks for credentials.
This test is that check.

Until 2026-09-27 shorter forms of the owner's name (first name alone, a short
first name with the surname, the surname alone) were still example data in
other tests and one code comment, because this check only knew the full name.
It now knows those forms too.

The owner's details are matched by SHA-256 of the lower-cased word or word
pair, so this file never contains them. Allowed on purpose:
- authorship credit (package authors, the MCPB manifest author and the report
  byline);
- placeholder home directories that name no one (``/Users/me``, ``/Users/x``,
  ``/home/alice`` and the others in ``_PLACEHOLDER_USERS``).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Authorship credit is public by design; the owner's name may appear only here.
_AUTHORSHIP_FILES = frozenset(
    {
        "pyproject.toml",
        "packaging/mcpb/manifest.json",
        "apps/api/src/alicebot_api/host_install.py",
        "tests/unit/test_host_install_writers.py",
        "docs/reports/provenance-grounded-memory.md",
    }
)

# SHA-256 of the owner's machine username, email, company domain and company
# name. Never allowed in any tracked file.
_FORBIDDEN_EVERYWHERE = frozenset(
    {
        "dc3b30837c3981a573659737abe402553cbed474a9275bdb2783e68eb64c7c99",
        "1f860f4c7a4347c3a65a9c5b1908ad21f59391b01d2b434eca8eedc7f279695b",
        "ba05c97f53c0a02f30b2e8013ef93f5f2f1be65b13c59e00dc538cf86be92b6d",
        "b238ab7f32142b982c3ccec21e93317d73820f029785a3070f70ed56909fb9c8",
    }
)
# SHA-256 of the owner's name and its shorter forms. Allowed only in
# authorship credit.
_FORBIDDEN_OUTSIDE_AUTHORSHIP = frozenset(
    {
        # full name
        "26fb9bf0496288f06cf41b78f83e86ea0d2ab3b452536bb93314acece73ccbe8",
        # short first name with the surname
        "c2646071b6b98e2db62764b17244b833ab225bf5317521130a0146b8e9724452",
        # surname alone
        "2eacdcd37486d80f1d8d892938eba56e7a2abcad2e64de2ffb905467cdda1085",
        # first name alone, two forms
        "24964a2764be8975c0958a4fa05e4040f9f76adb6f50bb8e290349ecc31f7a5f",
        "b816a16cd03774e0cefac03765680a33365d0b16060f67a2f7382a844f9c664f",
    }
)
# Name forms still in files not yet fixed, as (file, digest) pairs. The scan
# skips only that one form in that one file. Delete an entry when its file is
# fixed: test_known_name_leftovers_are_still_present fails until you do.
_KNOWN_NAME_LEFTOVERS = frozenset(
    {
        # a Telegram username in a smoke fixture
        (
            "apps/api/src/alicebot_api/cli/smokes.py",
            "b816a16cd03774e0cefac03765680a33365d0b16060f67a2f7382a844f9c664f",
        ),
    }
)

_WORD = re.compile(r"[A-Za-z0-9_@+-]+(?:\.[A-Za-z0-9_@+-]+)*")
_PLACEHOLDER_USERS = frozenset(
    {"me", "x", "alex", "Alex", "alice", "operator", "YOUR_USER", "you", "user", "example", "jane", "runner"}
)
_HOME_PATH = re.compile(r"(?:/Users|/home)/([A-Za-z][\w.-]*)/")


def _digests(text: str) -> set[str]:
    """SHA-256 of every lower-cased word, and of every pair of adjacent words."""

    words = [word.lower() for word in _WORD.findall(text)]
    candidates = set(words) | {f"{first} {second}" for first, second in zip(words, words[1:])}
    for word in words:
        # A path segment such as /Users/<name>/Desktop is one word to _WORD only
        # when it has no slash; split on the separators people put inside words.
        candidates.update(part for part in re.split(r"[/@]", word) if part)
    return {hashlib.sha256(candidate.encode()).hexdigest() for candidate in candidates}


def _home_path_users(text: str) -> set[str]:
    return {match.group(1) for match in _HOME_PATH.finditer(text)} - _PLACEHOLDER_USERS


def _tracked_text_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [name for name in listed.split("\0") if name]


def _read(relative: str) -> str | None:
    path = REPO_ROOT / relative
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    if b"\0" in data[:8192] or len(data) > 5_000_000:
        return None
    return data.decode("utf-8", errors="replace")


def test_the_matchers_catch_what_they_guard() -> None:
    """Keeps the repo scan honest, using made-up values in place of the real ones."""

    planted = {
        hashlib.sha256(b"someuser").hexdigest(),
        hashlib.sha256(b"someone@example.test").hexdigest(),
        hashlib.sha256(b"acme corp").hexdigest(),
    }
    assert planted <= _digests('"store": "/Users/someuser/Desktop/x.db"') | _digests(
        "ping someone@example.test today"
    ) | _digests("We met Acme Corp.")
    assert _home_path_users("store at /Users/somebody/project/db") == {"somebody"}
    assert _home_path_users("/home/someone/.config/alicebot/.env") == {"someone"}
    assert _home_path_users("see /Users/me/My Vault and /home/alice/notes") == set()
    assert len(_FORBIDDEN_EVERYWHERE) == 4 and len(_FORBIDDEN_OUTSIDE_AUTHORSHIP) == 5
    # A leftover can only excuse a name form, never a username, email or company.
    assert {digest for _, digest in _KNOWN_NAME_LEFTOVERS} <= _FORBIDDEN_OUTSIDE_AUTHORSHIP
    assert not {path for path, _ in _KNOWN_NAME_LEFTOVERS} & _AUTHORSHIP_FILES


def test_no_personal_details_or_local_home_paths_in_tracked_files() -> None:
    problems: list[str] = []
    for relative in _tracked_text_files():
        text = _read(relative)
        if text is None:
            continue
        digests = _digests(text)
        if digests & _FORBIDDEN_EVERYWHERE:
            problems.append(f"{relative}: owner username, email or company")
        leftovers = {digest for path, digest in _KNOWN_NAME_LEFTOVERS if path == relative}
        if relative not in _AUTHORSHIP_FILES and (digests & _FORBIDDEN_OUTSIDE_AUTHORSHIP) - leftovers:
            problems.append(f"{relative}: owner name outside authorship credit")
        if relative == "tests/unit/test_public_repo_hygiene.py":
            continue  # its made-up example paths exist to test the matcher
        for user in sorted(_home_path_users(text)):
            problems.append(f"{relative}: home path for user {user!r}")
    assert not problems, "Personal details or local paths in a public repo:\n" + "\n".join(problems)


def test_known_name_leftovers_are_still_present() -> None:
    """A leftover entry for a file that no longer has the name must be deleted."""

    stale = [path for path, digest in sorted(_KNOWN_NAME_LEFTOVERS) if digest not in _digests(_read(path) or "")]
    assert not stale, "Remove these entries from _KNOWN_NAME_LEFTOVERS:\n" + "\n".join(stale)
