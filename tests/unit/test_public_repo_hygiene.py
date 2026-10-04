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

Scratch paths are the second kind of local path checked. A builder's working
folder (``/private/tmp/...`` on macOS, ``/tmp/claude-...`` on Linux) written
into a living document or into code is a leak of how the work was done, and
the frozen handoff records of July 2026 hold several. Those records stay as
they are. Living files and code, tests and CI workflows included, may not name
a ``/private/tmp`` or ``/tmp`` path, with these explicit exceptions, each with
its reason beside it:

- ``_RECORD_PREFIXES`` and the released sections of ``CHANGELOG.md`` are dated
  records, frozen on purpose, and are not read. The list is pinned against a copy
  written out in ``test_the_record_exclusion_list_is_exactly_the_dated_record_folders``
  and against every tracked file, so dropping or adding a folder fails a test;
- ``_TMP_ALLOWED_VALUES`` is a closed list of the plain ``/tmp`` paths that are
  allowed, matched by exact value in any file, in groups that each carry a reason.
  A new ``/tmp`` path in a test, a script, a workflow or a doc fails until its value
  is on the list, so a made-up folder name from a builder's session cannot pass as
  a test fixture;
- ``_TMP_BARE_ALLOWED_FILES`` names the files where the bare folder ``/tmp`` is a
  stand-in or a runner folder;
- ``_PRIVATE_TMP_ALLOWED`` names the two tests that plant a leaked path to prove
  it gets redacted;
- a ``/tmp/claude-...`` or ``/private/tmp/claude-...`` path is never allowed
  in anything that is read, tests included.

Every exception must still be needed, or ``test_the_scratch_path_exceptions_are_all_still_needed``
fails until it is deleted.

Mutations that must fail the scratch-path tests, each alone: name a scratch path
in a living doc, in a code docstring, in the Unreleased changelog section or as a
session folder in a test file (``test_no_scratch_paths_in_living_files_and_code``);
name a made-up ``/tmp`` folder in a test, a script, a workflow or a doc (the same
test); drop a record folder from ``_RECORD_PREFIXES``, or add a living folder to it
(``test_the_record_exclusion_list_is_exactly_the_dated_record_folders``); read the
released changelog sections, or read them only when the heading starts ``## v``
(``test_dated_records_are_excluded_by_the_explicit_list_and_nothing_else_is``); lose
the ``/private`` prefix, or the end-of-sentence period, in the pattern; list an
exception no file uses. A path in a dated record or in a released changelog section
must not fail anything.
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
_KNOWN_NAME_LEFTOVERS: frozenset[tuple[str, str]] = frozenset()

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
        # Hyphenated and underscored forms are one word to _WORD. Split them
        # so each name part is checked on its own.
        candidates.update(part for part in re.split(r"[/@._-]", word) if part)
    return {hashlib.sha256(candidate.encode()).hexdigest() for candidate in candidates}


def _home_path_users(text: str) -> set[str]:
    return {match.group(1) for match in _HOME_PATH.finditer(text)} - _PLACEHOLDER_USERS


# --- scratch paths -----------------------------------------------------------

# Dated records, frozen on purpose: a scratch path in them is history, not a leak.
# Folder prefixes, matched whole, so ``docs/handoff-notes/`` is not excluded.
# ``docs/release/`` holds the release notes, checksums, checklists and tag plans.
# The released sections of CHANGELOG.md are excluded by _living_text.
# A copy of this list is written out in the test that pins it.
_RECORD_PREFIXES = (
    "docs/handoff/",
    "docs/archive/",
    "docs/release/",
    "docs/reports/",
    "docs/plans/",
)

# The plain ``/tmp`` paths that are allowed, by exact value, in any file that is read. Each group
# has the reason beside it. The list is closed: a ``/tmp`` path that is not here fails, in a test, a
# script, a workflow or a doc alike, and a value no file uses any more must be deleted.
_TMP_ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    (
        "the task workspace root and the artifact export root the code ships as defaults, the template that shows "
        "them, the scanner baseline that quotes them, and the paths tests build under them"
    ): (
        "/tmp/alicebot/",
        "/tmp/alicebot-vnext-artifact-exports",
        "/tmp/alicebot/task-workspaces",
        "/tmp/alicebot/task-workspaces/../escape",
        "/tmp/alicebot/task-workspaces/user/task",
        "/tmp/alicebot/task-workspaces/user/task/../escape.txt",
        "/tmp/alicebot/task-workspaces/user/task/docs/spec.txt",
        "/tmp/task-workspaces/",
        "/tmp/task-workspaces/task-1",
        "/tmp/task-workspaces/task-2",
        "/tmp/task-workspaces/task-3",
        "/tmp/workspace",
        "/tmp/workspace/task-1",
        "/tmp/task",
        "/tmp/docs/spec.txt",
        "/tmp/escape.txt",
        "/tmp/example.txt",
    ),
    "values that configuration tests set to prove an override is read": (
        "/tmp/custom-calendar-secrets",
        "/tmp/custom-gmail-secrets",
        "/tmp/custom-logs/alicebot.log",
        "/tmp/custom-workspaces",
        "/tmp/mapped-calendar-secrets",
        "/tmp/mapped-gmail-secrets",
        "/tmp/mapped-logs/alicebot.log",
        "/tmp/mapped-workspaces",
        "/tmp/test-calendar-secrets",
        "/tmp/test-gmail-secrets",
        "/tmp/test.db",
        "/tmp/alice.db",
    ),
    (
        "made-up vault, notes, project and home folders and made-up values in install, CLI, hook, guard and "
        "scanner tests, none of them a real folder"
    ): (
        "/tmp/vault",
        "/tmp/vault/memory.db",
        "/tmp/my-real-vault",
        "/tmp/alice-validator-vault",
        "/tmp/alice-data-dir-guard",
        "/tmp/notes",
        "/tmp/proj",
        "/tmp/home",
        "/tmp/work",
        "/tmp/x",
        "/tmp/fresh",
        "/tmp/capture",
        "/tmp/alice-secret",
        "/tmp/untrusted-ca.pem",
        "/tmp/alicebot/.venv/bin/python",
    ),
    "throwaway files that CI jobs and the Makefile write on a runner, and the tests that read those job definitions": (
        "/tmp/$",
        "/tmp/alicebot-python-coverage.json",
        "/tmp/eval.json",
        "/tmp/eval_gate.json",
        "/tmp/alice-release-body.md",
        "/tmp/alice-current-release.json",
        "/tmp/alice-current-release-body.md",
    ),
    "a command that a frozen handoff record shows, quoted by the test that pins that record": (
        "/tmp/alice-release-check.XXXXXX",
    ),
}
_TMP_VALUE_REASON: dict[str, str] = {
    path: reason for reason, paths in _TMP_ALLOWED_VALUES.items() for path in paths
}

# Files where the bare folder ``/tmp`` (no name under it) is a stand-in or a runner folder.
_TMP_BARE_ALLOWED_FILES: dict[str, tuple[str, ...]] = {
    "a stand-in folder that a test passes in, mocks or expects back, or a sample sentence that mentions one": (
        "tests/integration/test_api_logging_smoke.py",
        "tests/integration/test_provider_runtime_api.py",
        "tests/unit/test_codex_config_install.py",
        "tests/unit/test_hermes_memory_provider.py",
        "tests/unit/test_opencode_config_install.py",
        "tests/unit/test_per_project_hook.py",
        "tests/unit/test_recall_framing.py",
        "tests/unit/test_vnext_main.py",
        "tests/unit/fixtures_benign_corpus.py",
    ),
    "a script's own throwaway folder, or a stand-in home returned by a fake module": (
        "scripts/record_demo_vault_gif.py",
        "scripts/run_hermes_memory_provider_smoke.py",
    ),
    "the runner's folder in a CI job": (".github/workflows/security-scans.yml",),
}
_TMP_BARE_FILES = frozenset(path for paths in _TMP_BARE_ALLOWED_FILES.values() for path in paths)

# Single files where a ``/private/tmp`` path is deliberate.
_PRIVATE_TMP_ALLOWED: dict[str, str] = {
    "tests/unit/test_archive_maintenance.py": "plants a leaked path in an error to prove it is redacted",
    "tests/unit/test_hermes_memory_provider.py": "plants a leaked path in an error to prove it is redacted",
}

# A path under /tmp or /private/tmp, whole. The lookbehind skips a name that merely
# ends in a word, such as ``docs/tmp/x``, ``~/tmp``, ``$TMPDIR/tmp`` or ``./tmp``, and
# ``${TMPDIR:-/tmp}``. The folder name must not go on with a word character or a hyphen
# (``/tmpfile``, ``/tmp-old``) or with a period that starts an extension (``/tmp.json``),
# but a period that ends a sentence does not hide it (``written to /tmp.``). The path
# ends at white space, a quote, a backslash (a JSON or string escape) and the usual
# closing punctuation.
_SCRATCH_PATH = re.compile(
    r"(?<![\w.~$}-])(?P<path>(?:/private)?/tmp(?![\w-])(?!\.\w)(?:/[^\s\"'`\\<>()\[\]{},;:|&*?]*)?)"
)
_SESSION_SCRATCH = re.compile(r"(?:/private)?/tmp/claude-")
# This file's own made-up example paths exist to test the matchers.
_SCRATCH_SELF = "tests/unit/test_public_repo_hygiene.py"
# The Unreleased section and the title above it are living text. Any other level-two heading,
# whatever its shape (``## v0.21.0``, ``## [0.21.0]``, ``## 0.21.0``), starts a released section.
_RELEASED_SECTION = re.compile(r"(?m)^## (?!Unreleased\b)")


def _living_text(relative: str, text: str) -> str | None:
    """The part of a tracked file the scratch-path check reads, or None for a dated record."""

    if relative == _SCRATCH_SELF or relative.startswith(_RECORD_PREFIXES):
        return None
    if relative == "CHANGELOG.md":
        return _RELEASED_SECTION.split(text, maxsplit=1)[0]  # released sections are records
    return text


def _scratch_hits(relative: str, text: str) -> list[tuple[str, str]]:
    """Every scratch path a living file names, as (kind, path). Kind is session, private or tmp."""

    living = _living_text(relative, text)
    if living is None:
        return []
    hits: list[tuple[str, str]] = []
    for match in _SCRATCH_PATH.finditer(living):
        path = match.group("path").rstrip(".")  # a period after the path ends the sentence
        kind = "session" if _SESSION_SCRATCH.match(path) else "private" if path.startswith("/private") else "tmp"
        hits.append((kind, path))
    return hits


def _scratch_problems(relative: str, text: str) -> list[str]:
    """Scratch paths in a living file that no exception covers."""

    problems: list[str] = []
    for kind, path in sorted(set(_scratch_hits(relative, text))):
        if kind == "private" and relative in _PRIVATE_TMP_ALLOWED:
            continue
        if kind == "tmp" and (path in _TMP_VALUE_REASON or (path == "/tmp" and relative in _TMP_BARE_FILES)):
            continue
        problems.append(f"{relative}: scratch path {path!r} ({kind})")
    return problems


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
    # A hyphen, an underscore, or a dot must not hide the parts.
    joined = _digests("see ada-lovelace, ada_lovelace and ada.lovelace")
    assert hashlib.sha256(b"ada").hexdigest() in joined
    assert hashlib.sha256(b"lovelace").hexdigest() in joined
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


# --- scratch path tests ---------------------------------------------------------

# The record folders, written out again here on purpose and kept apart from
# _RECORD_PREFIXES: a test that read the module's list would pass whatever the list
# said. Changing either copy without the other fails
# test_the_record_exclusion_list_is_exactly_the_dated_record_folders.
_EXPECTED_RECORD_FOLDERS = (
    "docs/handoff/",
    "docs/archive/",
    "docs/release/",
    "docs/reports/",
    "docs/plans/",
)
# One file path in each living area, and the names that only start like a record folder.
_LIVING_SAMPLES = (
    "README.md",
    "RELEASING.md",
    "CONTRIBUTING.md",
    "docs/alpha/known-limitations.md",
    "docs/adr/ADR-001.md",
    "docs/examples/alice-memory-session-start.md",
    "docs/memory/promotion-personas.md",
    "docs/security/README.md",
    "docs/handoff-notes/a.md",
    "docs/handoff.md",
    "docs/archived/a.md",
    "docs/releases/a.md",
    "docs/release-notes/a.md",
    "docs/report/a.md",
    "docs/plan/a.md",
    "eval/longmemeval/count_probe.py",
    "packaging/mcpb/manifest.json",
    "scripts/tool.py",
    "apps/api/src/alicebot_api/example.py",
    "apps/web/lib/api.ts",
    "tests/unit/test_example.py",
    ".github/workflows/tests.yml",
)
_LEAK = "built in /private/tmp/alice-p2-package-final and /tmp/alice-batch16-package"


def test_the_scratch_matcher_catches_what_it_guards() -> None:
    """Mutation: stop reading a living doc or a docstring, drop /tmp or /private/tmp from the pattern,
    lose the end-of-sentence rule, or stop ending a path at a backslash.
    """

    doc = "docs/alpha/getting-started.md"
    code = "apps/api/src/alicebot_api/example.py"
    # A living document and a code docstring, in both spellings.
    assert _scratch_problems(doc, "Run it in /private/tmp/alice-run/out and read the log.")
    assert _scratch_problems(doc, "Run it in /tmp/alice-run/out and read the log.")
    assert _scratch_problems(code, '"""Writes its report to /private/tmp/alice-report.json."""')
    assert _scratch_problems(code, '"""Writes its report to /tmp/alice-report.json."""')
    assert _scratch_problems(code, "# scratch: /tmp")  # the bare folder is a path too
    assert _scratch_problems("scripts/tool.sh", 'cd "/private/tmp/x"')
    assert [path for _, path in _scratch_hits(doc, "see `/tmp/a/b.json`, then (/private/tmp/c).")] == [
        "/tmp/a/b.json",
        "/private/tmp/c",
    ]
    assert _scratch_hits(doc, "db sqlite:///tmp/test.db") == [("tmp", "/tmp/test.db")]
    # A period that ends the sentence is not part of the path, and does not hide the bare folder.
    assert _scratch_problems(doc, "The report is written to /tmp.")
    assert _scratch_problems(doc, "The report is written to /private/tmp.")
    assert _scratch_hits(doc, "The report is written to /tmp.\nIt is read from /private/tmp.") == [
        ("tmp", "/tmp"),
        ("private", "/private/tmp"),
    ]
    assert _scratch_hits(doc, "see /tmp/alice-batch16-package.") == [("tmp", "/tmp/alice-batch16-package")]
    assert _scratch_hits(doc, "see /tmp/a.b.json.") == [("tmp", "/tmp/a.b.json")]
    # A backslash ends a path, so a JSON or string escape is not part of it.
    assert _scratch_hits(doc, '{"p": "/tmp/alice-batch16-package\\\\"} and "/tmp/x\\0y"') == [
        ("tmp", "/tmp/alice-batch16-package"),
        ("tmp", "/tmp/x"),
    ]
    # A session scratch folder is never allowed, whatever the area or file.
    for relative in ("tests/unit/test_x.py", ".github/workflows/tests.yml", "Makefile", "docs/alpha/a.md"):
        assert _scratch_problems(relative, 'x = "/tmp/claude-1000/work"'), relative
        assert _scratch_problems(relative, 'x = "/private/tmp/claude-123/work"'), relative
    # Names that only contain "tmp" are not scratch paths.
    for text in (
        "docs/tmp/x.md and apps/tmp/y",
        "see ~/tmp/notes and ./tmp/out and $TMPDIR/tmp/z and ${HOME}/tmp",
        'mktemp "${TMPDIR:-/tmp}/alice-x.XXXXXX"',
        "/tmpfile /tmp.json /tmp-old/x /usr/tmp/x https://example.test/tmp/x",
        "the /private/tmpdir folder",
    ):
        assert _scratch_hits(doc, text) == [], text


def test_the_tmp_exceptions_are_exactly_what_they_say() -> None:
    """Mutation: let a file, an area or a test folder excuse a /tmp path that is not on the list, let a file
    excuse /private/tmp, let the bare folder pass in a file that is not named, or let a session folder pass.
    """

    made_up = 'x = "/tmp/alice-batch16-package"'
    # A made-up folder is refused in every kind of file, the ones that used to excuse any /tmp path included.
    for relative in (
        "tests/unit/test_config.py",
        "tests/integration/test_x.py",
        "apps/web/lib/api.test.ts",
        "apps/web/components/a.test.tsx",
        ".github/workflows/tests.yml",
        "Makefile",
        "RELEASING.md",
        ".env.example",
        "apps/api/src/alicebot_api/config.py",
        "scripts/record_demo_vault_gif.py",
        "scripts/measure_x.py",
        "docs/alpha/a.md",
        "README.md",
    ):
        assert _scratch_problems(relative, made_up), relative
    # A pinned value is allowed by its exact text only.
    pinned = "/tmp/vault"
    assert pinned in _TMP_VALUE_REASON
    assert _scratch_problems("tests/unit/test_host_install_writers.py", f'x = "{pinned}"') == []
    assert _scratch_problems("tests/unit/test_host_install_writers.py", f'x = "{pinned}2"')
    assert _scratch_problems("tests/unit/test_host_install_writers.py", f'x = "{pinned}/other"')
    assert _scratch_problems("tests/unit/test_host_install_writers.py", f'x = "{pinned[:-1]}"')
    # The bare folder is allowed in the named files and nowhere else.
    for relative in sorted(_TMP_BARE_FILES):
        assert _scratch_problems(relative, 'x = "/tmp"') == [], relative
    for relative in ("tests/unit/test_config.py", "docs/alpha/a.md", "scripts/measure_x.py", "README.md", "Makefile"):
        assert _scratch_problems(relative, 'x = "/tmp"'), relative
    # /private/tmp is excused for the two named tests and for nothing else, files that excuse /tmp included.
    private = 'x = "/private/tmp/fixture"'
    assert sorted(_PRIVATE_TMP_ALLOWED) == [
        "tests/unit/test_archive_maintenance.py",
        "tests/unit/test_hermes_memory_provider.py",
    ]
    for relative in _PRIVATE_TMP_ALLOWED:
        assert _scratch_problems(relative, private) == [], relative
        # They are excused for /private/tmp only, not for a made-up /tmp folder or a session folder.
        assert _scratch_problems(relative, made_up), relative
        assert _scratch_problems(relative, "/private/tmp/claude-123/x"), relative
    for relative in ("tests/unit/test_config.py", ".github/workflows/tests.yml", "Makefile", "RELEASING.md"):
        assert _scratch_problems(relative, private), relative


def test_the_record_exclusion_list_is_exactly_the_dated_record_folders() -> None:
    """Mutation: drop any one folder from ``_RECORD_PREFIXES``, add a living folder (``docs/examples/``,
    ``docs/memory/``, ``eval/``, ``packaging/``) or a looser prefix (``docs/``, ``docs/handoff``).

    The module's list is compared with a copy written out in this file, then each side is checked on its own:
    a path in every record folder is not read, a path in every living sample is, and every tracked file is
    read exactly when it is outside the record folders.
    """

    assert _RECORD_PREFIXES == _EXPECTED_RECORD_FOLDERS
    for folder in _EXPECTED_RECORD_FOLDERS:
        assert folder.endswith("/"), folder
        assert _scratch_problems(f"{folder}2026-07-16-example/BUILD_REPORT.md", _LEAK) == [], folder
        assert _scratch_problems(f"{folder}example.md", "x /tmp/claude-1000/y") == [], folder
    for relative in _LIVING_SAMPLES:
        assert not relative.startswith(_EXPECTED_RECORD_FOLDERS), relative
        assert _scratch_problems(relative, _LEAK), relative
    for relative in _tracked_text_files():
        if relative == _SCRATCH_SELF:
            continue  # its made-up example paths are never read
        in_record_folder = relative.startswith(_EXPECTED_RECORD_FOLDERS)
        assert (_living_text(relative, "x\n") is None) == in_record_folder, relative


def test_dated_records_are_excluded_by_the_explicit_list_and_nothing_else_is() -> None:
    """Mutation: read the released changelog sections, or read them only when the heading starts ``## v``,
    stop reading the Unreleased section or the title above it.
    """

    changelog = (
        "# Changelog\n\n## Unreleased\n\n- A living entry.\n\n"
        "## v0.20.0 \u2014 2026-10-02\n\n- Built in /private/tmp/alice-old.\n"
    )
    assert _scratch_problems("CHANGELOG.md", changelog) == []  # the leak is in a released section
    assert _scratch_problems("CHANGELOG.md", changelog.replace("A living entry.", "Ran in /tmp/alice-x.")), (
        "an Unreleased entry is living text"
    )
    assert _scratch_problems("CHANGELOG.md", "## Unreleased\n\n- Ran in /private/tmp/alice-x.\n")
    # A released heading of any shape starts a record, and the Unreleased section ends there.
    for heading in (
        "## v0.21.0 \u2014 2026-11-01",
        "## v0.21.0 - 2026-11-01",
        "## [0.21.0] - 2026-11-01",
        "## 0.21.0",
        "## Version 0.21.0",
        "## 2026-11-01",
    ):
        released = changelog.replace("## v0.20.0 \u2014 2026-10-02", heading)
        assert _scratch_problems("CHANGELOG.md", released) == [], heading
        assert _scratch_problems("CHANGELOG.md", released.replace("A living entry.", "Ran in /tmp/alice-x.")), heading
    # A subsection of Unreleased is still living text, and so is the title above it.
    assert _scratch_problems("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n### Fixed\n\n- Ran in /tmp/alice-x.\n")
    assert _scratch_problems("CHANGELOG.md", "# Changelog in /tmp/alice-x\n\n## Unreleased\n\n## v0.20.0\n")
    # This file's own example paths are not read.
    assert _scratch_problems(_SCRATCH_SELF, _LEAK) == []


def test_the_changelog_keeps_unreleased_first_so_released_sections_are_cut_off() -> None:
    """The cut at the first released heading only hides the released sections if Unreleased comes before it."""

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.count("\n## Unreleased\n") == 1
    released = _RELEASED_SECTION.search(changelog)
    assert released is not None, "the changelog has no released section"
    assert changelog.index("\n## Unreleased\n") < released.start()
    living = _living_text("CHANGELOG.md", changelog)
    assert living is not None
    assert "\n## Unreleased\n" in living
    assert changelog[released.start() :].splitlines()[0] not in living


def test_the_record_folders_are_real_tracked_folders() -> None:
    """A folder that matches nothing would excuse nothing and hide a typo."""

    tracked = _tracked_text_files()
    for folder in _EXPECTED_RECORD_FOLDERS:
        assert any(name.startswith(folder) for name in tracked), folder


def test_no_scratch_paths_in_living_files_and_code() -> None:
    problems: list[str] = []
    for relative in _tracked_text_files():
        text = _read(relative)
        if text is not None:
            problems.extend(_scratch_problems(relative, text))
    assert not problems, (
        "Scratch paths in living files or code. Use a placeholder, `$(mktemp -d)`, `tmp_path` or a relative path.\n"
        "A test fixture that must name a /tmp value goes on _TMP_ALLOWED_VALUES with its reason:\n"
        + "\n".join(problems)
    )


def test_the_scratch_path_exceptions_are_all_still_needed() -> None:
    """An exception no file uses any more must be deleted, so the lists never grow stale."""

    paths_by_file: dict[str, set[str]] = {}
    kinds_by_file: dict[str, set[str]] = {}
    for relative in _tracked_text_files():
        text = _read(relative)
        if text is not None:
            hits = _scratch_hits(relative, text)
            paths_by_file[relative] = {path for _, path in hits}
            kinds_by_file[relative] = {kind for kind, _ in hits}
    used = set().union(*paths_by_file.values())
    stale = [f"{path}: no file names it" for path in sorted(_TMP_VALUE_REASON) if path not in used]
    stale += [
        f"{path}: no bare /tmp left" for path in sorted(_TMP_BARE_FILES) if "/tmp" not in paths_by_file.get(path, ())
    ]
    stale += [
        f"{path}: no /private/tmp path left"
        for path in sorted(_PRIVATE_TMP_ALLOWED)
        if "private" not in kinds_by_file.get(path, ())
    ]
    assert not stale, "Delete these scratch-path exceptions:\n" + "\n".join(stale)
    # A reason for every group, a value in one group only, nothing that is a session folder or a record.
    reasons = (*_TMP_ALLOWED_VALUES, *_TMP_BARE_ALLOWED_FILES, *_PRIVATE_TMP_ALLOWED.values())
    assert all(reason.strip() for reason in reasons)
    flat = [path for paths in _TMP_ALLOWED_VALUES.values() for path in paths]
    assert len(flat) == len(set(flat)) == len(_TMP_VALUE_REASON), "a value is listed twice"
    assert all(path.startswith("/tmp/") for path in flat)
    assert not [path for path in flat if _SESSION_SCRATCH.match(path)]
    named_files = [path for paths in _TMP_BARE_ALLOWED_FILES.values() for path in paths]
    assert len(named_files) == len(set(named_files)), "a file is listed twice"
    assert not {*named_files, *_PRIVATE_TMP_ALLOWED} & {
        name for name in _tracked_text_files() if name.startswith(_RECORD_PREFIXES)
    }
