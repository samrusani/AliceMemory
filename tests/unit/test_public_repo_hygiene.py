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
they are. Living files and code may not name a ``/private/tmp`` or ``/tmp``
path, with these explicit exceptions, each with its reason beside it:

- ``_RECORD_PREFIXES`` and the released sections of ``CHANGELOG.md`` are dated
  records, frozen on purpose, and are not read;
- ``_TMP_ALLOWED_AREAS`` and ``_TMP_ALLOWED_FILES`` name where ``/tmp`` is a
  made-up test input or a real temporary folder in a command or a default;
- ``_PRIVATE_TMP_ALLOWED`` names the two tests that plant a leaked path to
  prove it gets redacted;
- a ``/tmp/claude-...`` or ``/private/tmp/claude-...`` path is never allowed
  in anything that is read, tests included.

Every exception must still be needed, or ``test_the_scratch_path_exceptions_are_all_still_needed``
fails until it is deleted.

Mutations that must fail the scratch-path tests, each alone: name a scratch path
in a living doc, in a code docstring, in the Unreleased changelog section or as a
session folder in a test file (``test_no_scratch_paths_in_living_files_and_code``);
drop a record folder from ``_RECORD_PREFIXES`` (the same test, on the records' own
paths); read the released changelog sections; lose the ``/private`` prefix in the
pattern; list an exception no file uses. A path in a dated record or in a released
changelog section must not fail anything.
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
_RECORD_PREFIXES = (
    "docs/handoff/",
    "docs/archive/",
    "docs/release/",
    "docs/reports/",
    "docs/plans/",
)

# Where a plain ``/tmp`` path is allowed: a made-up input in a test, or a real
# throwaway folder on a CI runner. Matched against the start (or the end, for the
# test-file suffixes) of the path.
_TMP_ALLOWED_AREAS: dict[str, str] = {
    "tests/": "test code: made-up inputs and expected values, not notes about anyone's machine",
    ".test.ts": "web test code: made-up inputs and expected values",
    ".test.tsx": "web test code: made-up inputs and expected values",
    ".github/workflows/": "CI jobs write throwaway files to the runner's /tmp",
}

# Single files where a plain ``/tmp`` path is a real runtime location or a made-up input.
_TMP_ALLOWED_FILES: dict[str, str] = {
    ".bandit-baseline.json": "generated scanner baseline that quotes the two source defaults below",
    ".env.example": "template that shows the TASK_WORKSPACE_ROOT default",
    "Makefile": "default output file of the local coverage check",
    "RELEASING.md": "the release procedure's own commands make throwaway files; test_control_doc_truth pins one",
    "apps/api/src/alicebot_api/config.py": "DEFAULT_TASK_WORKSPACE_ROOT, a runtime default",
    "apps/api/src/alicebot_api/vnext_queue.py": "DEFAULT_VNEXT_ARTIFACT_EXPORT_ROOT, a runtime default",
    "scripts/fuzz_codex_config_writer.py": "made-up project path inside a fuzzed config fixture",
    "scripts/record_demo_vault_gif.py": "makes a throwaway demo data folder under /tmp",
    "scripts/run_hermes_memory_provider_smoke.py": "stand-in Hermes home returned by a fake module",
}

# Single files where a ``/private/tmp`` path is deliberate.
_PRIVATE_TMP_ALLOWED: dict[str, str] = {
    "tests/unit/test_archive_maintenance.py": "plants a leaked path in an error to prove it is redacted",
    "tests/unit/test_hermes_memory_provider.py": "plants a leaked path in an error to prove it is redacted",
}

# A path under /tmp or /private/tmp, whole. The lookbehind skips a name that merely
# ends in a word, such as ``docs/tmp/x``, ``~/tmp``, ``$TMPDIR/tmp`` or ``./tmp``.
_SCRATCH_PATH = re.compile(r"(?<![\w.~$}-])(?P<path>(?:/private)?/tmp(?![\w.-])(?:/[^\s\"'`<>()\[\]{},;:|&*?]*)?)")
_SESSION_SCRATCH = re.compile(r"(?:/private)?/tmp/claude-")
# This file's own made-up example paths exist to test the matchers.
_SCRATCH_SELF = "tests/unit/test_public_repo_hygiene.py"
_UNRELEASED_END = re.compile(r"(?m)^## v\d")


def _living_text(relative: str, text: str) -> str | None:
    """The part of a tracked file the scratch-path check reads, or None for a dated record."""

    if relative == _SCRATCH_SELF or relative.startswith(_RECORD_PREFIXES):
        return None
    if relative == "CHANGELOG.md":
        return _UNRELEASED_END.split(text, maxsplit=1)[0]  # released sections are records
    return text


def _scratch_hits(relative: str, text: str) -> list[tuple[str, str]]:
    """Every scratch path a living file names, as (kind, path). Kind is session, private or tmp."""

    living = _living_text(relative, text)
    if living is None:
        return []
    hits: list[tuple[str, str]] = []
    for match in _SCRATCH_PATH.finditer(living):
        path = match.group("path")
        kind = "session" if _SESSION_SCRATCH.match(path) else "private" if path.startswith("/private") else "tmp"
        hits.append((kind, path))
    return hits


def _in_tmp_area(relative: str) -> str | None:
    """The allowed area a path falls in, else None."""

    for area in _TMP_ALLOWED_AREAS:
        if relative.startswith(area) if area.endswith("/") else relative.endswith(area):
            return area
    return None


def _scratch_problems(relative: str, text: str) -> list[str]:
    """Scratch paths in a living file that no exception covers."""

    problems: list[str] = []
    for kind, path in sorted(set(_scratch_hits(relative, text))):
        if kind == "private" and relative in _PRIVATE_TMP_ALLOWED:
            continue
        if kind == "tmp" and (relative in _TMP_ALLOWED_FILES or _in_tmp_area(relative)):
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


def test_the_scratch_matcher_catches_what_it_guards() -> None:
    """Mutation: stop reading a living doc or a docstring, drop /tmp or /private/tmp from the pattern."""

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
    # A session scratch folder is never allowed, whatever the area or file.
    for relative in ("tests/unit/test_x.py", ".github/workflows/tests.yml", "Makefile", "docs/alpha/a.md"):
        assert _scratch_problems(relative, 'x = "/tmp/claude-1000/work"'), relative
        assert _scratch_problems(relative, 'x = "/private/tmp/claude-123/work"'), relative
    # Names that only contain "tmp" are not scratch paths.
    for text in (
        "docs/tmp/x.md and apps/tmp/y",
        "see ~/tmp/notes and ./tmp/out and $TMPDIR/tmp/z and ${HOME}/tmp",
        "/tmpfile /tmp.json /tmp-old/x /usr/tmp/x https://example.test/tmp/x",
        "the /private/tmpdir folder",
    ):
        assert _scratch_hits(doc, text) == [], text


def test_the_exceptions_for_tmp_and_private_tmp_are_exactly_what_they_say() -> None:
    """Mutation: let an area or a file excuse /private/tmp, or let a test or workflow excuse a session folder."""

    plain = 'x = "/tmp/fixture"'
    private = 'x = "/private/tmp/fixture"'
    # A plain /tmp is excused in the named areas and files only.
    for relative in (
        "tests/unit/test_config.py",
        "apps/web/lib/api.test.ts",
        "apps/web/components/a.test.tsx",
        ".github/workflows/tests.yml",
        "Makefile",
        "apps/api/src/alicebot_api/config.py",
    ):
        assert _scratch_problems(relative, plain) == [], relative
    for relative in ("docs/alpha/a.md", "apps/api/src/alicebot_api/example.py", "scripts/measure_x.py", "README.md"):
        assert _scratch_problems(relative, plain), relative
    # /private/tmp is excused for the two named tests and for nothing else, areas and files included.
    assert _scratch_problems("tests/unit/test_archive_maintenance.py", private) == []
    assert _scratch_problems("tests/unit/test_hermes_memory_provider.py", private) == []
    for relative in ("tests/unit/test_config.py", ".github/workflows/tests.yml", "Makefile", "RELEASING.md"):
        assert _scratch_problems(relative, private), relative
    # The two tests are excused for /private/tmp only, not for a session folder.
    assert _scratch_problems("tests/unit/test_archive_maintenance.py", "/private/tmp/claude-123/x")


def test_dated_records_are_excluded_by_the_explicit_list_and_nothing_else_is() -> None:
    """Mutation: drop a record folder from the list, match it by a loose prefix, or read released changelog sections."""

    leak = "built in /private/tmp/alice-p2-package-final and /tmp/alice-batch16-package"
    for prefix in _RECORD_PREFIXES:
        assert _scratch_problems(f"{prefix}2026-07-16-example/BUILD_REPORT.md", leak) == [], prefix
        assert _scratch_problems(f"{prefix}example.md", "x /tmp/claude-1000/y") == [], prefix
    # The list is exact: a folder that only starts the same way, and the living docs, are read.
    for relative in (
        "docs/handoff-notes/a.md",
        "docs/archived/a.md",
        "docs/releases/a.md",
        "docs/adr/ADR-001.md",
        "docs/security/README.md",
        "docs/alpha/known-limitations.md",
        "README.md",
    ):
        assert _scratch_problems(relative, leak), relative

    changelog = (
        "# Changelog\n\n## Unreleased\n\n- A living entry.\n\n"
        "## v0.20.0 \u2014 2026-10-02\n\n- Built in /private/tmp/alice-old.\n"
    )
    assert _scratch_problems("CHANGELOG.md", changelog) == []  # the leak is in a released section
    assert _scratch_problems("CHANGELOG.md", changelog.replace("A living entry.", "Ran in /tmp/alice-x.")), (
        "an Unreleased entry is living text"
    )
    assert _scratch_problems("CHANGELOG.md", "## Unreleased\n\n- Ran in /private/tmp/alice-x.\n")
    # This file's own example paths are not read.
    assert _scratch_problems(_SCRATCH_SELF, leak) == []


def test_the_record_prefixes_are_real_tracked_folders() -> None:
    """A prefix that matches nothing would excuse nothing and hide a typo."""

    tracked = _tracked_text_files()
    for prefix in _RECORD_PREFIXES:
        assert any(name.startswith(prefix) for name in tracked), prefix
    assert (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").count("\n## Unreleased\n") == 1


def test_no_scratch_paths_in_living_files_and_code() -> None:
    problems: list[str] = []
    for relative in _tracked_text_files():
        text = _read(relative)
        if text is not None:
            problems.extend(_scratch_problems(relative, text))
    assert not problems, (
        "Scratch paths in living files or code. Use a placeholder, `$(mktemp -d)` or a relative path, or add a\n"
        "documented exception at the top of the scratch section of this test:\n" + "\n".join(problems)
    )


def test_the_scratch_path_exceptions_are_all_still_needed() -> None:
    """An exception no file uses any more must be deleted, so the lists never grow stale."""

    kinds_by_file: dict[str, set[str]] = {}
    for relative in _tracked_text_files():
        text = _read(relative)
        if text is not None:
            kinds_by_file[relative] = {kind for kind, _ in _scratch_hits(relative, text)}
    stale = [
        f"{path}: no /tmp path left" for path in sorted(_TMP_ALLOWED_FILES) if "tmp" not in kinds_by_file.get(path, ())
    ]
    stale += [
        f"{path}: no /private/tmp path left"
        for path in sorted(_PRIVATE_TMP_ALLOWED)
        if "private" not in kinds_by_file.get(path, ())
    ]
    stale += [
        f"{area}: no file in this area names /tmp"
        for area in sorted(_TMP_ALLOWED_AREAS)
        if not any("tmp" in kinds and _in_tmp_area(path) == area for path, kinds in kinds_by_file.items())
    ]
    assert not stale, "Delete these scratch-path exceptions:\n" + "\n".join(stale)
    # One reason each, and no file listed twice over.
    reasons = (*_TMP_ALLOWED_AREAS.values(), *_TMP_ALLOWED_FILES.values(), *_PRIVATE_TMP_ALLOWED.values())
    assert all(reason.strip() for reason in reasons)
    assert not [path for path in _TMP_ALLOWED_FILES if _in_tmp_area(path)], "a file is both listed and in an area"
    assert not {*_TMP_ALLOWED_FILES, *_PRIVATE_TMP_ALLOWED} & {
        name for name in _tracked_text_files() if name.startswith(_RECORD_PREFIXES)
    }
