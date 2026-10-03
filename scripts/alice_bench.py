#!/usr/bin/env python3
"""alice_bench: measure what an agent reads when it searches an imported notes folder.

This is the measurement harness of the search-quality release. It builds a fresh
vault from a folder of Markdown files with the importer the checkout ships, runs
``alice_recall`` the way an MCP host would, and scores what came back against
anchors: verbatim strings that a correct answer needs.

Commands, all of which take ``--run-dir`` (a directory this harness owns):

* ``build``: import a corpus folder into a fresh vault under the run directory, in
  a chosen order, and write the corpus snapshot that the grep arm searches.
* ``recall``: print the text ``alice_recall`` returns for one query.
* ``batch``: run every question of a question set, both query variants, and save
  the outputs with a fingerprint of the run.
* ``anchors``: check that every anchor of a question set occurs verbatim in the
  snapshot of a built vault.
* ``search``: the budgeted per-arm command an answering agent runs. Three searches,
  a file-locked counter and an append-only log.
* ``score``: Tier 1. Free and deterministic. Prints how many questions have all
  their anchors in the first 4 KB and 8 KB of what the agent would read, beside a
  negative control.
* ``fingerprint``: print the fingerprint of a run.

Tier 1 is necessary and not sufficient. An anchor in the output is stricter than
"the right file came back" and weaker than a correct answer.

The harness calls private internals of the checkout (``MCPServer._handle_request``,
the importer's snapshot reader), so a refactor of those fails a test in CI.
POSIX only: the search counter uses ``fcntl``.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATES_PATH = REPO_ROOT / "gates.json"

QUESTIONS_SCHEMA = "alice-bench-questions/1"
OUTPUTS_SCHEMA = "alice-bench-outputs/1"
MANIFEST_SCHEMA = "alice-bench-run/1"
RUN_MARKER = ".alice_bench_run"
VAULT_DIRNAME = "vault"
VAULT_FILENAME = "memory.db"
SNAPSHOT_DIRNAME = "snapshot"
STATE_DIRNAME = "state"
MANIFEST_FILENAME = "manifest.json"

# Every variable whose name starts with this is removed from a run. The prefix
# covers ALICE_* (the product switches, the embeddings endpoint, the agent key)
# and ALICEBOT_* (the settings of the API process).
ENV_PREFIX = "ALICE"
SEARCH_QUALITY_ENV = "ALICE_SEARCH_QUALITY"

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
VARIANTS = ("verbatim", "keyword")

RECALL_TOOL = "alice_recall"

# The Alice arm keeps these three at the tool's own defaults. The wrapper sends
# only the query and refuses any other value, so the claim can name the setting.
# A call that leaves one out gets the tool's default, which is what the
# fingerprint records for it; what a call really carried is recorded as sent.
PINNED_RECALL_DEFAULTS: Mapping[str, object] = {
    "limit": 8,
    "context_depth": "low",
    "include_sources": True,
}

# A fingerprint reads its recall settings off the calls a session sent. A session that
# has sent none yet (the ``fingerprint`` command opens a fresh one) sends this one, an
# ordinary word, through the same path every recall takes.
FINGERPRINT_PROBE_QUERY = "notes"

# Tier 1 scores text that came out of the vault and nothing else. Leaves are
# named by a path in which each list index is written "[]".
SCORED_LEAVES = frozenset({"sources[].excerpt", "results[].text"})
# Every other string leaf a recall can carry is metadata, the echoed question or
# an id. A leaf in neither list stops the scorer, so a new field has to be
# decided on purpose. The debug trace ("retrieval") is deliberately absent: the
# Alice arm never asks for it.
UNSCORED_LEAVES = frozenset(
    {
        "framing",
        "query",
        "results[].id",
        "results[].type",
        "results[].domain",
        "results[].status",
        "results[].writer.id",
        "results[].writer.established",
        "results[].validity.valid_from",
        "results[].validity.valid_to",
        "results[].validity.superseded_by_memory_id",
        "results[].validity.supersedes_memory_id",
        "results[].validity.corrected_at",
        "sources[].id",
        "sources[].source_type",
        "sources[].title",
        "sources[].captured_at",
        "sources[].domain",
        "sources[].sensitivity",
        "sources[].excerpt_kind",
        "sources[].current_memory_id",
        "sources[].writer.id",
        "sources[].writer.established",
        "entities[].id",
        "entities[].name",
        "entities[].entity_type",
    }
)

EXIT_OK = 0
EXIT_SEARCH_ERROR = 1
EXIT_REFUSED = 2
EXIT_BUDGET = 3

GREP_TIMEOUT_SECONDS = 30
_GREP_LETTERS = frozenset("inwxEFlLcohHvsIrR")
_GREP_COUNT_OPTIONS = frozenset({"-A", "-B", "-C", "-m"})


class BenchError(Exception):
    """A harness failure whose message is safe to print."""


class CheckoutError(BenchError):
    pass


class RunDirError(BenchError):
    pass


class AnchorError(BenchError):
    pass


class UnclassifiedLeafError(BenchError):
    pass


class PinnedSettingError(BenchError):
    pass


class BudgetExhausted(BenchError):
    pass


class GrepOptionError(BenchError):
    pass


# --------------------------------------------------------------------------
# Small helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to one space. Quoting flattens newlines."""

    return " ".join(text.split())


def unquote_leaf(value: str) -> str:
    """Undo the quoting layer of a recall excerpt.

    Recall wraps stored text in JSON quotes after it flattens whitespace, so a
    quote character in the source arrives as a backslash and a quote. The
    scorer compares the text the file holds, so it reads the layer back off.
    """

    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            inner = json.loads(value)
        except ValueError:
            return value
        if isinstance(inner, str):
            return inner
    return value


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------
# Gates


@dataclass(frozen=True)
class Gates:
    data: dict[str, Any]
    sha256: str
    path: Path

    @property
    def budgets(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.data["tier1"]["byte_budgets"])

    @property
    def gate_budget(self) -> int:
        return int(self.data["tier1"]["gate_budget_bytes"])

    @property
    def searches_per_run(self) -> int:
        return int(self.data["search"]["searches_per_run"])

    @property
    def grep_cap(self) -> int:
        return int(self.data["search"]["grep_cap_bytes"])

    @property
    def anchor_min_length(self) -> int:
        return int(self.data["tier1"]["anchor_min_length"])

    @property
    def anchor_max_files(self) -> int:
        return int(self.data["tier1"]["anchor_max_files"])

    @property
    def import_orders(self) -> tuple[str, ...]:
        return tuple(str(value) for value in self.data["import_orders"])

    @property
    def prompt_hashes(self) -> dict[str, str | None]:
        prompts = self.data["hashes"]["prompts"]
        return {str(key): (None if value is None else str(value)) for key, value in prompts.items()}


def load_gates(path: Path | None = None) -> Gates:
    target = DEFAULT_GATES_PATH if path is None else path
    raw = target.read_bytes()
    data = json.loads(raw)
    if data.get("schema") != "alice-bench-gates/1":
        raise BenchError(f"{target.name} is not an alice-bench-gates/1 file")
    return Gates(data=data, sha256=sha256_bytes(raw), path=target)


# --------------------------------------------------------------------------
# Environment


def scrub_environment(env: Mapping[str, str], *, allow: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``env`` with every ALICE* variable removed, then the arm's own set.

    An agent key or an embeddings endpoint in the parent must never reach a run:
    a key changes who the caller is, and an endpoint changes what a recall does.
    """

    clean = {name: value for name, value in env.items() if not name.startswith(ENV_PREFIX)}
    clean.update(allow or {})
    return clean


@contextlib.contextmanager
def scoped_environment(allow: Mapping[str, str] | None = None) -> Iterator[list[str]]:
    """Scrub ``os.environ`` for the length of the block and restore it after.

    Yields the sorted names that were removed. Never the values.
    """

    saved = dict(os.environ)
    removed = sorted(name for name in saved if name.startswith(ENV_PREFIX))
    os.environ.clear()
    os.environ.update(scrub_environment(saved, allow=allow))
    try:
        yield removed
    finally:
        os.environ.clear()
        os.environ.update(saved)


def arm_environment(search_quality: str | None) -> dict[str, str]:
    """The only ALICE variables a run may carry."""

    return {} if search_quality is None else {SEARCH_QUALITY_ENV: search_quality}


# --------------------------------------------------------------------------
# The checkout under test


def src_dir(repo: Path) -> Path:
    return repo / "apps" / "api" / "src"


def activate_checkout(repo: Path) -> Path:
    """Put the checkout's source first on the path and prove the import came from it.

    The venv of a worktree usually imports the main checkout, so the path is set
    here and the real path of ``alicebot_api.__file__`` is checked afterwards.
    Returns that real path.
    """

    repo = repo.resolve()
    source = src_dir(repo)
    if not (source / "alicebot_api" / "__init__.py").is_file():
        raise CheckoutError("the checkout has no apps/api/src/alicebot_api package")
    if "alicebot_api" not in sys.modules:
        sys.path.insert(0, str(source))
    import alicebot_api

    actual = Path(os.path.realpath(alicebot_api.__file__))
    if not actual.is_relative_to(source.resolve()):
        raise CheckoutError(
            "alicebot_api was already imported from outside the checkout; "
            "start a new process with --checkout"
        )
    return actual


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckoutError(f"the checkout has a .git and git could not be run on it ({type(exc).__name__})") from exc


# What the manifest and the fingerprint say when a checkout has no .git of its own, as an exported
# copy of a commit does. It is a plain statement, never a failure: the content hash identifies the code.
NO_GIT = "no git"
WITH_GIT = "present"


def git_state(repo: Path) -> dict[str, object]:
    """The commit of the checkout and whether it differs from that commit, when it has a .git.

    Dirty means a tracked file differs from HEAD, or an untracked file sits under
    apps/ or workers/, because either changes what the product code does.

    A ``.git`` is looked for at the root of the checkout itself, as a folder or as the file a git
    worktree has, and nowhere above it: ``git -C`` would walk up into any repository that happens
    to contain an exported copy and report that repository's commit as the copy's own. A checkout
    with no ``.git`` there is reported as ``no git`` with no commit and no dirty flag. A ``.git``
    that git cannot read a commit from is a refusal, because a commit that cannot be read is a
    commit that is silently missing from the record.
    """

    if not os.path.lexists(repo / ".git"):
        return {"git": NO_GIT, "git_sha": None, "dirty": None}
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CheckoutError("the checkout has a .git that git cannot read a commit from")
    tracked = _git(repo, "diff", "--quiet", "HEAD")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "--", "apps", "workers")
    dirty = tracked.returncode != 0 or bool(untracked.stdout.strip())
    return {"git": WITH_GIT, "git_sha": head.stdout.strip(), "dirty": dirty}


# The only parts of the source tree that are not hashed. A ``__pycache__`` folder is where the
# interpreter writes compiled copies of the files beside it, and a harness run writes some, so a
# hash that read it would differ between the build and the next command. A compiled file that
# sits outside a ``__pycache__`` folder is importable on its own, so it is hashed like any other.
_SOURCE_HASH_SKIPPED_DIRS = frozenset({"__pycache__"})
_SOURCE_HASH_SKIPPED_FILES = frozenset({".DS_Store"})


def checkout_source_sha256(repo: Path) -> str:
    """A hash of the code a run imports: every file under ``apps/api/src``, in a stable order.

    The hash covers each file's path (POSIX, relative to ``apps/api/src``) and its bytes, each
    written after its length so that no two trees can give the same stream. It needs no git, so an
    exported copy of a commit and a worktree of the same commit give the same value, and it moves
    with every edit, which a commit and a dirty flag cannot do once the checkout is dirty. A symlink
    in the tree is refused, because its target is not part of the tree. Installed dependencies
    and the files outside ``apps/api/src`` are not covered.
    """

    root = src_dir(repo.resolve())
    if not root.is_dir():
        raise CheckoutError("the checkout has no apps/api/src to hash")
    found: dict[str, Path] = {}
    for current, dirnames, filenames in os.walk(root):
        base = Path(current)
        kept: list[str] = []
        for name in dirnames:
            if base.joinpath(name).is_symlink():
                raise CheckoutError(f"apps/api/src holds a symlink ({name}), which the source hash cannot follow")
            if name not in _SOURCE_HASH_SKIPPED_DIRS:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = base / name
            if name in _SOURCE_HASH_SKIPPED_FILES:
                continue
            if path.is_symlink():
                raise CheckoutError(f"apps/api/src holds a symlink ({name}), which the source hash cannot follow")
            if not path.is_file():
                raise CheckoutError(f"apps/api/src holds {name}, which is not a plain file the source hash can read")
            found[path.relative_to(root).as_posix()] = path
    digest = hashlib.sha256()
    for relative in sorted(found):
        try:
            data = found[relative].read_bytes()
        except OSError as exc:
            raise CheckoutError(f"apps/api/src/{relative} cannot be read ({type(exc).__name__})") from exc
        name_bytes = relative.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


# What says that two processes ran the same checkout: whether it has a git history and, if so,
# its commit and whether it differs from that commit; a hash of the source it imports; and the
# real path ``alicebot_api`` was imported from. The hash is the one that cannot be fooled by a
# second edit on a dirty tree or by the absence of git.
CHECKOUT_IDENTITY_KEYS = ("git", "git_sha", "dirty", "checkout_source_sha256", "alicebot_api_file")


def checkout_identity(repo: Path) -> dict[str, object]:
    """The identity of the checkout this process runs, in the keys of ``CHECKOUT_IDENTITY_KEYS``."""

    import alicebot_api

    state = git_state(repo)
    return {
        "git": state["git"],
        "git_sha": state["git_sha"],
        "dirty": state["dirty"],
        "checkout_source_sha256": checkout_source_sha256(repo),
        "alicebot_api_file": str(Path(os.path.realpath(alicebot_api.__file__))),
    }


def require_matching_build(manifest: Mapping[str, Any] | None, repo: Path) -> Mapping[str, Any]:
    """Refuse to read a vault that another checkout built. Returns the manifest it checked.

    A vault is rebuilt for every checkout and never cached, because the chunker is one of
    the things a release can change. The manifest records which checkout built the vault,
    and a command that reads it has to be running the same one: the same source content,
    the same commit and dirty state when there is a git history, and the same
    ``alicebot_api`` on disk.
    """

    if manifest is None:
        raise RunDirError("the run directory holds no build; run build first")
    recorded = manifest.get("build")
    if not isinstance(recorded, dict) or any(key not in recorded for key in CHECKOUT_IDENTITY_KEYS):
        raise CheckoutError(
            "the manifest does not say which checkout built this vault (it may come from an older harness); "
            "build it again with --rebuild"
        )
    current = checkout_identity(repo)
    differing = [key for key in CHECKOUT_IDENTITY_KEYS if recorded[key] != current[key]]
    if differing:
        raise CheckoutError(
            "this vault was built by a different checkout ("
            + ", ".join(differing)
            + " differ); a vault is rebuilt for every checkout, so build it again with --rebuild"
        )
    return manifest


# --------------------------------------------------------------------------
# Run directories


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def prepare_run_dir(run_dir: Path, *, create: bool) -> Path:
    """The resolved run directory. It is new, empty, or one this harness made."""

    resolved = run_dir.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise RunDirError("the run directory is not a directory")
        entries = list(resolved.iterdir())
        if entries and not (resolved / RUN_MARKER).exists():
            raise RunDirError("the run directory is not empty and is not a harness run directory")
        if create and not entries:
            (resolved / RUN_MARKER).write_text("alice_bench run directory\n", encoding="utf-8")
        return resolved
    if not create:
        raise RunDirError("the run directory does not exist")
    resolved.mkdir(parents=True)
    (resolved / RUN_MARKER).write_text("alice_bench run directory\n", encoding="utf-8")
    return resolved


def require_run_dir(run_dir: Path) -> Path:
    resolved = run_dir.resolve()
    if not (resolved / RUN_MARKER).is_file():
        raise RunDirError("the run directory was not made by this harness")
    return resolved


def resolve_inside(run_dir: Path, requested: str | None, *, default: Path, what: str) -> Path:
    """``requested`` or ``default``, refused when it is outside the run directory."""

    target = default if requested is None else Path(requested)
    resolved = target.resolve()
    if not _is_inside(resolved, run_dir):
        raise RunDirError(f"{what} is outside the run directory")
    return resolved


# What the harness keeps in a run directory for itself. A vault may not sit on any of
# these, because a rebuild deletes the vault folder and must never take the marker, the
# manifest, the snapshot or an answering run's counter with it.
_RESERVED_RUN_ENTRIES = frozenset({SNAPSHOT_DIRNAME, STATE_DIRNAME, MANIFEST_FILENAME, RUN_MARKER})


def check_vault_location(run_dir: Path, data_dir: Path) -> Path:
    """``data_dir`` when it is a folder of its own inside ``run_dir``, else a refusal.

    It cannot be the run directory itself (a rebuild would delete the directory and its
    marker) and it cannot be, or sit under, a name the harness keeps for itself.
    """

    resolved = data_dir.resolve()
    if not _is_inside(resolved, run_dir):
        raise RunDirError("--data-dir is outside the run directory")
    if resolved == run_dir:
        raise RunDirError("--data-dir cannot be the run directory itself; name a folder inside it")
    reserved = resolved.relative_to(run_dir).parts[0]
    if reserved in _RESERVED_RUN_ENTRIES:
        raise RunDirError(f"--data-dir cannot be {reserved}, which the harness keeps for itself")
    return resolved


def vault_path(data_dir: Path) -> Path:
    return data_dir / VAULT_FILENAME


# --------------------------------------------------------------------------
# Corpus and build


@dataclass(frozen=True)
class CorpusFile:
    relative_path: str
    path: Path
    text: str
    raw_sha256: str


def read_corpus(corpus_dir: Path) -> tuple[Path, list[CorpusFile]]:
    """The Markdown files the importer would select, read once, sorted by path."""

    from alicebot_api.importer_paths import DEFAULT_MAX_TEXT_FILE_BYTES
    from alicebot_api.markdown_import import _snapshot_markdown_source

    try:
        folder, snapshot = _snapshot_markdown_source(corpus_dir, max_file_bytes=DEFAULT_MAX_TEXT_FILE_BYTES)
    except (OSError, ValueError) as exc:  # the importer's own refusal: no Markdown, a file too large, unreadable
        raise BenchError(f"the corpus folder cannot be read: {exc}") from exc
    files = [
        CorpusFile(
            relative_path=item.relative_path,
            path=item.path,
            text=item.text,
            raw_sha256=sha256_file(item.path),
        )
        for item in snapshot
    ]
    files.sort(key=lambda item: item.relative_path)
    if not files:
        raise BenchError("the corpus folder holds no Markdown files")
    return folder, files


def corpus_hash(files: Sequence[CorpusFile]) -> str:
    lines = [f"{item.relative_path}\0{item.raw_sha256}" for item in sorted(files, key=lambda f: f.relative_path)]
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def order_files(files: Sequence[CorpusFile], order: str) -> list[CorpusFile]:
    """Capture order: ``sorted``, ``reverse`` or ``shuffle:SEED``.

    The recency list of recall breaks ties by capture time, so the order a vault
    was built in can move which documents it lists. A result is shown under
    several orders for that reason.
    """

    ordered = sorted(files, key=lambda item: item.relative_path)
    if order == "sorted":
        return ordered
    if order == "reverse":
        return list(reversed(ordered))
    if order.startswith("shuffle:"):
        try:
            seed = int(order.split(":", 1)[1])
        except ValueError as exc:
            raise BenchError("shuffle needs an integer seed, as in shuffle:7") from exc
        random.Random(seed).shuffle(ordered)
        return ordered
    raise BenchError("order must be sorted, reverse or shuffle:SEED")


def _import_one(db_path: Path, file_path: Path, *, domain: str, sensitivity: str) -> dict[str, Any]:
    from alicebot_api import onramp

    stdout, stderr = io.StringIO(), io.StringIO()
    argv = [
        "import-markdown",
        "--db",
        str(db_path),
        "--from",
        str(file_path),
        "--domain",
        domain,
        "--sensitivity",
        sensitivity,
    ]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = onramp.main(argv)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    if code != 0 or not lines:
        raise BenchError(f"importing {file_path.name} failed (exit {code})")
    record = json.loads(lines[-1])
    if not isinstance(record, dict):
        raise BenchError(f"importing {file_path.name} printed no batch record")
    return record


def read_vault_sources(db_path: Path) -> list[dict[str, Any]]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT title, raw_path, external_id, metadata_json FROM sources "
            "WHERE deleted_at IS NULL ORDER BY captured_at, id"
        ).fetchall()
    finally:
        conn.close()
    sources: list[dict[str, Any]] = []
    for title, raw_path, external_id, metadata_json in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        sources.append(
            {
                "title": title,
                "raw_path": raw_path,
                "external_id": external_id,
                "metadata": metadata,
            }
        )
    return sources


def vault_row_counts(db_path: Path) -> dict[str, int]:
    """Row counts of every table, event_log included. A recall must change none."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts: dict[str, int] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    finally:
        conn.close()
    return counts


def vault_identity(db_path: Path) -> dict[str, Any]:
    """What the vault holds, read from the vault alone and without writing to it.

    The count of live sources, the count of chunks, and one hash over the live sources in the order
    they were captured, each as its corpus path, its title and a hash of its stored text. A vault
    built from another corpus, in another import order, or by a chunker that cut it differently gives
    another value, which the manifest of the build can then be held to.
    """

    sources = read_vault_sources(db_path)
    lines: list[str] = []
    for source in sources:
        text = source["metadata"].get("raw_text")
        if not isinstance(text, str):
            raise BenchError("a stored source holds no raw_text; the vault cannot be identified")
        lines.append(f"{source['external_id']}\0{source['title']}\0{sha256_bytes(text.encode('utf-8'))}")
    return {
        "sources": len(sources),
        "chunks": vault_row_counts(db_path).get("source_chunks", 0),
        "vault_text_sha256": sha256_bytes("\n".join(lines).encode("utf-8")),
    }


def require_matching_vault(manifest: Mapping[str, Any], run_dir: Path, data_dir: Path) -> None:
    """Refuse to read a vault, or a grep snapshot, that the manifest does not describe.

    The manifest records the folder the vault was built into, what that vault holds and what the
    snapshot holds. A command that reads either has to find exactly that: the same folder (a rebuild
    into another ``--data-dir`` leaves the old folder on disk), the same sources in the same capture
    order with the same chunk count (a vault copied over another one under the same name), and the
    same snapshot files. Each is a refusal and none is repaired, because the two arms must read the
    text the manifest names.
    """

    recorded_dir = manifest.get("vault_dir")
    if not isinstance(recorded_dir, str) or any(
        key not in manifest for key in ("sources", "chunks", "vault_text_sha256", "snapshot_hash")
    ):
        raise RunDirError(
            "the manifest does not say which vault folder it describes or what that vault holds "
            "(it may come from an older harness); build again with --rebuild"
        )
    current_dir = data_dir.relative_to(run_dir).as_posix()
    if recorded_dir != current_dir:
        raise RunDirError(
            f"this run was built into the vault folder {recorded_dir!r} and not {current_dir!r}; "
            "point --data-dir at the folder the manifest names, or build again with --rebuild"
        )
    db_path = vault_path(data_dir)
    if not db_path.is_file():
        raise RunDirError(f"the vault folder {current_dir!r} holds no {VAULT_FILENAME}; build again with --rebuild")
    actual = vault_identity(db_path)
    differing = [key for key in ("sources", "chunks", "vault_text_sha256") if actual[key] != manifest[key]]
    if differing:
        raise RunDirError(
            "the vault does not hold what the manifest says it was built with ("
            + ", ".join(differing)
            + " differ); it was replaced or changed after the build, so build again with --rebuild"
        )
    snapshot_dir = run_dir / SNAPSHOT_DIRNAME
    if not snapshot_dir.is_dir() or snapshot_folder_hash(snapshot_dir) != manifest["snapshot_hash"]:
        raise RunDirError(
            "the grep snapshot does not hold what the manifest says it was built with; "
            "it was replaced or changed after the build, so build again with --rebuild"
        )


def require_visible_labels(*, domain: str, sensitivity: str) -> None:
    """Refuse labels that would hide the corpus from recall while grep still reads it.

    Both arms must read the same text. Recall answers a keyless caller up to the default
    sensitivity ceiling, so a file imported above it would be in the grep snapshot and
    absent from every recall. The sensitive domains are held back from a project brief,
    so they are refused the same way rather than left to chance. Both lists are read from
    the checkout under test, which keeps the harness from keeping a copy of its own.
    """

    from alicebot_api import vnext_agent_control, vnext_memory_commit

    ceiling = tuple(getattr(vnext_agent_control, "DEFAULT_AGENT_SENSITIVITY", ()))
    held_back = frozenset(getattr(vnext_memory_commit, "SENSITIVE_DOMAINS", ()))
    if not ceiling:
        raise CheckoutError("this checkout does not say what sensitivity recall answers by default")
    if sensitivity not in ceiling:
        raise BenchError(
            f"--sensitivity {sensitivity} is above what recall answers by default ({', '.join(ceiling)}), "
            "so grep would read files that recall cannot return"
        )
    if domain in held_back:
        raise BenchError(
            f"--domain {domain} is one of the sensitive domains a project view holds back, "
            "so the two arms might not read the same text"
        )


def build_vault(
    *,
    corpus_dir: Path,
    run_dir: Path,
    data_dir: Path,
    order: str,
    domain: str,
    sensitivity: str,
    rebuild: bool,
    repo: Path,
    search_quality: str | None = None,
) -> dict[str, Any]:
    """Build a fresh vault and the grep snapshot under ``run_dir``. Returns the manifest.

    The vault is never cached: the chunker is one of the things a release can
    change. Files are imported one at a time in the requested order, each by the
    same ``alice-memory import-markdown`` code a person would run. The manifest
    records which checkout built the vault, the folder it was built into and what the
    vault and the snapshot hold, and every later command refuses to read it from another
    checkout or to read a folder or a snapshot that does not match
    (``require_matching_build``, ``require_matching_vault``).
    """

    data_dir = check_vault_location(run_dir, data_dir)
    require_visible_labels(domain=domain, sensitivity=sensitivity)
    folder, files = read_corpus(corpus_dir)
    ordered = order_files(files, order)
    snapshot_dir = run_dir / SNAPSHOT_DIRNAME
    manifest_path = run_dir / MANIFEST_FILENAME
    existing = [path for path in (data_dir, snapshot_dir, manifest_path) if path.exists()]
    if existing and not rebuild:
        raise RunDirError("the run directory already holds a build; use --rebuild to replace it")
    for path in existing:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    data_dir.mkdir()
    db_path = vault_path(data_dir)

    receipts: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for item in ordered:
        record = _import_one(db_path, item.path, domain=domain, sensitivity=sensitivity)
        receipts.append(record)
        labels = [str(label) for label in record.get("skipped_credential_items", [])]
        if labels:
            withheld.append({"file": item.relative_path, "items": labels})
    snapshot_files = export_snapshot(db_path, snapshot_dir, corpus_root=folder if folder.is_dir() else folder.parent)
    held = vault_identity(db_path)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "built_at": utc_now(),
        "build": {**checkout_identity(repo), "search_quality": search_quality if search_quality is not None else "unset"},
        "order": order,
        "vault_dir": data_dir.relative_to(run_dir).as_posix(),
        "capture_order": [entry["relative_path"] for entry in snapshot_files],
        "domain": domain,
        "sensitivity": sensitivity,
        "corpus_hash": corpus_hash(files),
        "corpus_file_count": len(files),
        "snapshot_hash": snapshot_hash({entry["relative_path"]: entry["text"] for entry in snapshot_files}),
        "sources": held["sources"],
        "chunks": held["chunks"],
        "vault_text_sha256": held["vault_text_sha256"],
        "withheld": withheld,
        "duplicates": sum(1 for record in receipts if int(record.get("imported_count", 0)) == 0),
        "harness_sha256": sha256_file(Path(__file__)),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def export_snapshot(db_path: Path, snapshot_dir: Path, *, corpus_root: Path) -> list[dict[str, str]]:
    """Write the stored text of every live source under ``snapshot_dir``.

    The grep arm searches this folder, so it can never see a line the importer
    withheld from the vault. Returns the files in capture order.
    """

    root = corpus_root.resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []
    for source in read_vault_sources(db_path):
        text = source["metadata"].get("raw_text")
        if not isinstance(text, str):
            raise BenchError("a stored source holds no raw_text; the grep snapshot cannot be built")
        raw_path = source["raw_path"]
        try:
            relative = Path(str(raw_path)).resolve().relative_to(root).as_posix()
        except (ValueError, TypeError):
            relative = str(source["metadata"].get("relative_path") or source["external_id"] or source["title"])
        target = snapshot_dir / relative
        if not _is_inside(target.resolve(), snapshot_dir.resolve()):
            raise BenchError("a source path leaves the snapshot folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append({"relative_path": relative, "text": text})
    return written


def _snapshot_digest(file_hashes: Mapping[str, str]) -> str:
    lines = [f"{name}\0{digest}" for name, digest in sorted(file_hashes.items())]
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def snapshot_hash(files: Mapping[str, str]) -> str:
    return _snapshot_digest({name: sha256_bytes(text.encode("utf-8")) for name, text in files.items()})


def snapshot_folder_hash(snapshot_dir: Path) -> str:
    """``snapshot_hash`` of the files as they are on disk now, read as bytes.

    Bytes and not text, so that the hash never depends on how a reader treats line endings or
    encodings. For the folder a build wrote this is equal to the ``snapshot_hash`` the build recorded.
    """

    return _snapshot_digest(
        {
            path.relative_to(snapshot_dir).as_posix(): sha256_bytes(path.read_bytes())
            for path in snapshot_dir.rglob("*")
            if path.is_file()
        }
    )


def read_snapshot(snapshot_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file():
            files[path.relative_to(snapshot_dir).as_posix()] = path.read_text(encoding="utf-8")
    return files


# --------------------------------------------------------------------------
# Questions and anchors


@dataclass(frozen=True)
class Anchor:
    text: str
    file: str
    approved: str | None = None


@dataclass(frozen=True)
class Fact:
    label: str
    anchors: tuple[Anchor, ...]


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    keyword_query: str | None
    kind: str | None
    facts: tuple[Fact, ...]

    def query_for(self, variant: str) -> str:
        if variant == "verbatim":
            return self.question
        if variant == "keyword":
            if not self.keyword_query:
                raise BenchError(f"question {self.id} has no keyword query")
            return self.keyword_query
        raise BenchError(f"unknown variant {variant}")


@dataclass(frozen=True)
class QuestionSet:
    set_id: str
    questions: tuple[Question, ...]
    sha256: str


def load_questions(path: Path) -> QuestionSet:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != QUESTIONS_SCHEMA:
        raise BenchError(f"{path.name} is not an {QUESTIONS_SCHEMA} file")
    questions: list[Question] = []
    seen: set[str] = set()
    for entry in data.get("questions", []):
        qid = str(entry["id"])
        if qid in seen:
            raise BenchError(f"duplicate question id {qid}")
        seen.add(qid)
        facts: list[Fact] = []
        for fact in entry["facts"]:
            anchors = tuple(
                Anchor(text=str(a["text"]), file=str(a["file"]), approved=a.get("approved"))
                for a in fact["anchors"]
            )
            if not anchors or any(not anchor.text.strip() for anchor in anchors):
                raise BenchError(f"question {qid} has a fact with no usable anchor")
            facts.append(Fact(label=str(fact.get("fact", "")), anchors=anchors))
        if not facts:
            raise BenchError(f"question {qid} has no facts")
        questions.append(
            Question(
                id=qid,
                question=str(entry["question"]),
                keyword_query=(None if entry.get("keyword_query") is None else str(entry["keyword_query"])),
                kind=(None if entry.get("kind") is None else str(entry["kind"])),
                facts=tuple(facts),
            )
        )
    if not questions:
        raise BenchError("the question set is empty")
    return QuestionSet(set_id=str(data.get("set_id", path.stem)), questions=tuple(questions), sha256=sha256_bytes(raw))


def verify_anchors(qset: QuestionSet, snapshot: Mapping[str, str], gates: Gates) -> dict[str, Any]:
    """Check every anchor against the snapshot. Raises ``AnchorError`` on a miss.

    A miss means the anchor is not verbatim in the file it names, so a corpus
    change or a typo cannot pass quietly. The report also records, for each
    anchor, its length, how many files hold it and whether the question holds
    it, and flags the ones a person has to read.
    """

    normalized = {name: normalize_ws(text) for name, text in snapshot.items()}
    missing: list[str] = []
    rows: list[dict[str, Any]] = []
    for question in qset.questions:
        own_text = normalize_ws(question.question)
        own_keywords = normalize_ws(question.keyword_query or "")
        for fact_index, fact in enumerate(question.facts, start=1):
            for anchor in fact.anchors:
                text = normalize_ws(anchor.text)
                named = normalized.get(anchor.file)
                where = f"{question.id} fact {fact_index} in {anchor.file}"
                if named is None:
                    missing.append(f"{where}: no such file in the snapshot")
                    continue
                if text not in named:
                    missing.append(f"{where}: not verbatim")
                    continue
                files_holding = sum(1 for body in normalized.values() if text in body)
                flags: list[str] = []
                if len(text) < gates.anchor_min_length:
                    flags.append("short")
                if files_holding > gates.anchor_max_files:
                    flags.append("many_files")
                in_question = text in own_text
                in_keywords = text in own_keywords
                if in_question or in_keywords:
                    flags.append("in_question")
                rows.append(
                    {
                        "question": question.id,
                        "fact": fact_index,
                        "length": len(text),
                        "files": files_holding,
                        "in_question": in_question,
                        "in_keyword_query": in_keywords,
                        "flags": flags,
                        "approved": anchor.approved,
                    }
                )
    if missing:
        raise AnchorError("anchors that are not verbatim in the corpus snapshot:\n" + "\n".join(missing))
    flagged = [row for row in rows if row["flags"]]
    return {
        "anchors": len(rows),
        "flagged": len(flagged),
        "flagged_unapproved": sum(1 for row in flagged if not row["approved"]),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# Reading a recall: leaves, offsets, bytes


@dataclass(frozen=True)
class Leaf:
    path: str
    value: str
    end_byte: int


def locate_leaves(text: str) -> list[Leaf]:
    """Every string leaf of a recall result with the byte offset where it ends.

    The text is re-emitted in the order it was parsed and compared with the
    original, so an offset can only come from text that really has that shape.
    """

    parsed = json.loads(text)
    pieces: list[str] = []
    leaves: list[Leaf] = []
    position = 0

    def write(chunk: str) -> None:
        nonlocal position
        pieces.append(chunk)
        position += len(chunk.encode("utf-8"))

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            write("{")
            for index, (key, child) in enumerate(value.items()):
                if index:
                    write(",")
                write(json.dumps(key))
                write(":")
                walk(child, f"{path}.{key}" if path else str(key))
            write("}")
        elif isinstance(value, list):
            write("[")
            for index, child in enumerate(value):
                if index:
                    write(",")
                walk(child, f"{path}[]")
            write("]")
        elif isinstance(value, str):
            write(json.dumps(value))
            leaves.append(Leaf(path=path, value=value, end_byte=position))
        else:
            write(json.dumps(value))

    walk(parsed, "")
    if "".join(pieces) != text:
        raise BenchError("the recall text is not compact JSON in parse order; leaves cannot be placed")
    return leaves


def classify_leaves(leaves: Sequence[Leaf]) -> None:
    """Fail closed: every string leaf is scored or known not to be."""

    unknown = sorted({leaf.path for leaf in leaves if leaf.path not in SCORED_LEAVES and leaf.path not in UNSCORED_LEAVES})
    if unknown:
        raise UnclassifiedLeafError("unclassified recall leaf: " + ", ".join(unknown))


@dataclass(frozen=True)
class OutputScore:
    found: tuple[bool, ...]
    bytes_total: int
    documents: int
    passages: int

    @property
    def hit(self) -> bool:
        return all(self.found)


def score_output(text: str, facts: Sequence[Fact], *, budget: int) -> OutputScore:
    """Which facts have an anchor in a scored string that ends inside the first ``budget`` bytes.

    The budget counts the whole serialized output, because that is what an agent
    pays: the framing line, then every key in order, so a long ``entities`` list
    ahead of ``sources`` spends it.
    """

    leaves = locate_leaves(text)
    classify_leaves(leaves)
    readable = [
        normalize_ws(unquote_leaf(leaf.value))
        for leaf in leaves
        if leaf.path in SCORED_LEAVES and leaf.end_byte <= budget
    ]
    found = tuple(
        any(
            needle in body
            for anchor in fact.anchors
            if (needle := normalize_ws(anchor.text))
            for body in readable
        )
        for fact in facts
    )
    parsed = json.loads(text)
    sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
    source_ids = {str(entry.get("id")) for entry in sources if isinstance(entry, dict)}
    return OutputScore(
        found=found,
        bytes_total=len(text.encode("utf-8")),
        documents=len(source_ids),
        passages=len(sources),
    )


def score_set(
    outputs: Mapping[str, Mapping[str, str]],
    qset: QuestionSet,
    *,
    variant: str,
    budget: int,
) -> dict[str, Any]:
    """Tier 1 for one variant at one budget, with the negative control.

    The control scores each question's anchors against the next question's
    output (a rotation by one). A set whose floor is not near zero has a leak or
    weak anchors.
    """

    questions = qset.questions
    hits = 0
    facts_found = 0
    facts_total = 0
    floor_hits = 0
    bytes_sum = 0
    documents_sum = 0
    passages_sum = 0
    per_question: dict[str, bool] = {}
    for index, question in enumerate(questions):
        text = outputs[question.id][variant]
        result = score_output(text, question.facts, budget=budget)
        per_question[question.id] = result.hit
        hits += 1 if result.hit else 0
        facts_found += sum(1 for flag in result.found if flag)
        facts_total += len(question.facts)
        bytes_sum += result.bytes_total
        documents_sum += result.documents
        passages_sum += result.passages
        if len(questions) > 1:
            neighbour = questions[(index + 1) % len(questions)]
            control = score_output(outputs[neighbour.id][variant], question.facts, budget=budget)
            floor_hits += 1 if control.hit else 0
    count = len(questions)
    return {
        "n": count,
        "hits": hits,
        "facts_found": facts_found,
        "facts_total": facts_total,
        "floor_hits": floor_hits if count > 1 else None,
        "bytes_per_call": bytes_sum / count,
        "documents_per_call": documents_sum / count,
        "passages_per_call": passages_sum / count,
        "per_question": per_question,
    }


def score_outputs_document(doc: Mapping[str, Any], qset: QuestionSet, gates: Gates) -> dict[str, Any]:
    if doc.get("schema") != OUTPUTS_SCHEMA:
        raise BenchError(f"not an {OUTPUTS_SCHEMA} file")
    if doc.get("question_set_sha256") not in (None, qset.sha256):
        raise BenchError("the outputs were made for a different question set")
    outputs = doc["outputs"]
    for question in qset.questions:
        if question.id not in outputs:
            raise BenchError(f"the outputs hold nothing for question {question.id}")
    report: dict[str, Any] = {"set_id": qset.set_id, "variants": {}}
    for variant in VARIANTS:
        if not all(variant in outputs[q.id] for q in qset.questions):
            continue
        report["variants"][variant] = {
            str(budget): score_set(outputs, qset, variant=variant, budget=budget) for budget in gates.budgets
        }
    return report


def format_report(label: str, report: Mapping[str, Any], gates: Gates, *, per_question: bool) -> str:
    lines = [f"Tier 1, {label}, set {report['set_id']}"]
    lines.append(f"{'variant':9} {'budget':>7} {'hits':>9} {'facts':>11} {'floor':>9} {'bytes/call':>11} {'docs/call':>10}")
    for variant, by_budget in report["variants"].items():
        for budget in gates.budgets:
            cell = by_budget[str(budget)]
            floor = "n/a" if cell["floor_hits"] is None else f"{cell['floor_hits']}/{cell['n']}"
            lines.append(
                f"{variant:9} {budget:>7} {cell['hits']:>4}/{cell['n']:<4} "
                f"{cell['facts_found']:>5}/{cell['facts_total']:<5} {floor:>9} "
                f"{cell['bytes_per_call']:>11.0f} {cell['documents_per_call']:>10.2f}"
            )
    if per_question:
        for variant, by_budget in report["variants"].items():
            cell = by_budget[str(gates.gate_budget)]
            lines.append(f"per question, {variant}, {gates.gate_budget} bytes (dev only)")
            for qid, flag in cell["per_question"].items():
                lines.append(f"  {qid} {'hit' if flag else 'miss'}")
    return "\n".join(lines)


def minimum_across_orders(reports: Sequence[tuple[str, Mapping[str, Any]]], gates: Gates) -> dict[str, dict[str, int]]:
    """The lowest hit count per variant and budget over the import orders.

    The arm rule reads membership at 8 KB and never position, and it takes the
    minimum over the orders, because the order moves what the recency list shows.
    """

    minimums: dict[str, dict[str, int]] = {}
    for variant in VARIANTS:
        for budget in gates.budgets:
            values = [
                int(report["variants"][variant][str(budget)]["hits"])
                for _, report in reports
                if variant in report["variants"]
            ]
            if values:
                minimums.setdefault(variant, {})[str(budget)] = min(values)
    return minimums


# Fingerprint keys that must be equal in every outputs file of one comparison: the same
# checkout (its commit when it has one, and the hash of its source in any case), switch,
# corpus, snapshot, question set, gates and harness. Only the import
# order may differ, and it has to.
SAME_ACROSS_ORDERS = (
    "git_sha",
    "dirty",
    "alicebot_api_file",
    "checkout_source_sha256",
    "tools_list_digest",
    "search_quality",
    "recall_arguments",
    "recall_limit",
    "recall_context_depth",
    "recall_include_sources",
    "corpus_hash",
    "snapshot_hash",
    "question_set_sha256",
    "gates_sha256",
    "harness_sha256",
)


def require_comparable_outputs(documents: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    """Refuse to take a minimum over outputs that did not measure the same thing.

    The minimum over import orders is only meaningful across one checkout, one switch,
    one corpus and one question set. Two outputs files from different commits, or from
    one order twice, would give a number that names neither. A single file has nothing
    to be compared with.
    """

    if len(documents) < 2:
        return
    fingerprints: list[tuple[str, Mapping[str, Any]]] = []
    for name, document in documents:
        fp = document.get("fingerprint")
        if not isinstance(fp, dict):
            raise BenchError(f"{name} has no fingerprint, so it cannot be compared with the other outputs")
        missing = [key for key in (*SAME_ACROSS_ORDERS, "import_order") if key not in fp]
        if missing:
            raise BenchError(f"the fingerprint of {name} lacks {', '.join(missing)}")
        fingerprints.append((name, fp))
    first_name, first = fingerprints[0]
    for name, fp in fingerprints[1:]:
        differing = [key for key in SAME_ACROSS_ORDERS if fp[key] != first[key]]
        if differing:
            raise BenchError(
                f"{name} and {first_name} did not measure the same thing ({', '.join(differing)} differ); "
                "the minimum over import orders is read across one checkout, switch, corpus and question set"
            )
    orders = [str(fp["import_order"]) for _, fp in fingerprints]
    if len(set(orders)) != len(orders):
        raise BenchError("two outputs files come from the same import order; each one must come from its own")


def without_per_question(report: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``report`` with the per-question hits and misses left out.

    They are a dev-only view that a person asks for with ``--per-question``.
    """

    copy = json.loads(json.dumps(report))
    for by_budget in copy["variants"].values():
        for cell in by_budget.values():
            cell.pop("per_question", None)
    return dict(copy)


# --------------------------------------------------------------------------
# The MCP session


class McpSession:
    """The checkout's MCP server, called in process the way a host calls it over stdio.

    ``_handle_request`` is what the stdio loop calls for each message, so the text
    here is the text a host would put in front of the model.
    """

    def __init__(self, db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> None:
        from uuid import UUID

        from alicebot_api import onramp
        from alicebot_api.mcp_server import MCPServer
        from alicebot_api.mcp_tools import MCPRuntimeContext

        context = MCPRuntimeContext(database_url=onramp.sqlite_url_for_path(db_path), user_id=UUID(user_id))
        self._server = MCPServer(context=context, input_stream=io.BytesIO(), output_stream=io.BytesIO())
        self._next_id = 1
        # The arguments of every alice_recall call this session sent, as they reached the
        # server. The fingerprint reads the recall settings from here and from nothing else.
        self.recall_calls: list[dict[str, Any]] = []

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            request["params"] = params
            if method == "tools/call" and params.get("name") == RECALL_TOOL:
                self.recall_calls.append(copy.deepcopy(dict(params.get("arguments") or {})))
        self._next_id += 1
        response = self._server._handle_request(request)
        if response is None:
            raise BenchError(f"the server returned nothing for {method}")
        return response

    def tools_list(self) -> list[dict[str, Any]]:
        response = self._request("tools/list")
        tools = response["result"]["tools"]
        return [dict(tool) for tool in tools]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """``(is_error, text)``: the one text block the host would show the model."""

        response = self._request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            return True, json.dumps(response["error"], sort_keys=True)
        result = response["result"]
        text = "".join(str(block.get("text", "")) for block in result.get("content", []))
        return bool(result.get("isError")), text

    def recall(self, query: str) -> str:
        is_error, text = self.call(RECALL_TOOL, alice_arm_arguments(query))
        if is_error:
            raise BenchError("alice_recall returned an error: " + text[:200])
        return text


def alice_arm_arguments(
    query: str,
    *,
    limit: int | None = None,
    context_depth: str | None = None,
    include_sources: bool | None = None,
    **extra: object,
) -> dict[str, Any]:
    """The arguments the Alice arm sends: the query and nothing else.

    ``limit``, ``context_depth`` and ``include_sources`` stay at the tool's
    defaults. Another value is refused, so the claim can name the setting and an
    answering agent cannot widen its own window.
    """

    requested = {"limit": limit, "context_depth": context_depth, "include_sources": include_sources}
    for name, value in requested.items():
        if value is not None and value != PINNED_RECALL_DEFAULTS[name]:
            raise PinnedSettingError(f"{name} is pinned to {PINNED_RECALL_DEFAULTS[name]!r} in the Alice arm")
    if extra:
        raise PinnedSettingError("the Alice arm takes only a query: " + ", ".join(sorted(extra)))
    return {"query": query}


def recall_settings(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What a set of ``alice_recall`` calls carried, read off the calls themselves.

    ``arguments`` lists each distinct set of arguments sent, without the query (``[{}]``
    when every call sent the query alone). Each of the three pinned settings is the value
    the calls sent, or the tool's own default for a call that left it out: one value when
    every call agrees, the sorted distinct values when they do not.
    """

    if not calls:
        raise BenchError("no alice_recall call was sent, so there are no recall settings to record")
    arguments = sorted(
        {canonical_json({name: value for name, value in call.items() if name != "query"}) for call in calls}
    )
    settings: dict[str, Any] = {"arguments": [json.loads(item) for item in arguments]}
    for name, default in PINNED_RECALL_DEFAULTS.items():
        distinct = sorted({canonical_json(call.get(name, default)) for call in calls})
        settings[name] = json.loads(distinct[0]) if len(distinct) == 1 else [json.loads(item) for item in distinct]
    return settings


def tools_list_digest(tools: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_json(list(tools)).encode("utf-8"))


# --------------------------------------------------------------------------
# Fingerprint


def fingerprint(
    *,
    repo: Path,
    gates: Gates,
    session: McpSession | None,
    manifest: Mapping[str, Any] | None,
    search_quality: str | None,
    question_set_sha256: str | None,
    answerer_model: str | None = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """What a number measured. Not ``alicebot_api.__version__``: that reads installed
    distribution metadata and can name a different build than the tree on the path.

    The recall settings are read off the ``alice_recall`` calls the session sent. A session
    that has sent none yet sends one probe call first, so the fields always describe a real call.
    """

    identity = checkout_identity(repo)
    real_file = Path(str(identity["alicebot_api_file"]))
    tools = session.tools_list() if session is not None else None
    recall: dict[str, Any] | None = None
    if session is not None:
        if not session.recall_calls:
            session.recall(FINGERPRINT_PROBE_QUERY)
        recall = recall_settings(session.recall_calls)
    return {
        "git": identity["git"],
        "git_sha": identity["git_sha"],
        "dirty": identity["dirty"],
        "checkout_source_sha256": identity["checkout_source_sha256"],
        "alicebot_api_file": str(real_file),
        "alicebot_api_inside_checkout": real_file.is_relative_to(src_dir(repo.resolve()).resolve()),
        "vault_build": None if manifest is None else manifest.get("build"),
        "tools_list_digest": None if tools is None else tools_list_digest(tools),
        "search_quality": search_quality if search_quality is not None else "unset",
        "recall_arguments": None if recall is None else recall["arguments"],
        "recall_limit": None if recall is None else recall["limit"],
        "recall_context_depth": None if recall is None else recall["context_depth"],
        "recall_include_sources": None if recall is None else recall["include_sources"],
        "byte_budgets": list(gates.budgets),
        "grep_cap_bytes": gates.grep_cap,
        "corpus_hash": None if manifest is None else manifest.get("corpus_hash"),
        "snapshot_hash": None if manifest is None else manifest.get("snapshot_hash"),
        "question_set_sha256": question_set_sha256,
        "import_order": None if manifest is None else manifest.get("order"),
        "answerer_model": answerer_model,
        "judge_model": judge_model,
        "prompt_hashes": gates.prompt_hashes,
        "gates_sha256": gates.sha256,
        "harness_sha256": sha256_file(Path(__file__)),
    }


# --------------------------------------------------------------------------
# The budgeted search command


@dataclass(frozen=True)
class SearchResult:
    text: str
    status: str
    truncated: bool
    raw_bytes: int


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        return 0


def run_budgeted_search(
    state_dir: Path,
    *,
    arm: str,
    search_input: str,
    budget: int,
    runner: Callable[[], SearchResult],
) -> SearchResult:
    """Admit one search, run it and log it, all under one file lock.

    The attempt is counted before it runs, so a search that fails (a bad grep
    pattern, a tool error, any exception) still spends one of the searches and
    still writes its log line, so the count and the log never disagree. A refused
    search writes no log line. The counter and the log live in ``state_dir``, which
    belongs to one answering run, so two processes of one run share one counter.
    """

    count_path = state_dir / "search.count"
    log_path = state_dir / "search_log.jsonl"
    with _locked(state_dir / "search.lock"):
        used = _read_count(count_path)
        if used >= budget:
            raise BudgetExhausted(f"search budget of {budget} is used")
        tmp = count_path.with_suffix(".tmp")
        tmp.write_text(str(used + 1), encoding="utf-8")
        os.replace(tmp, count_path)
        try:
            result = runner()
        except BenchError as exc:
            result = SearchResult(text=f"error: {exc}", status="error", truncated=False, raw_bytes=0)
        except Exception as exc:  # any failure of a search is logged and counted, never a traceback
            result = SearchResult(
                text=f"error: the search failed ({type(exc).__name__})", status="error", truncated=False, raw_bytes=0
            )
        entry = {
            "n": used + 1,
            "at": utc_now(),
            "arm": arm,
            "input": search_input,
            "status": result.status,
            "bytes": result.raw_bytes,
            "truncated": result.truncated,
            "sha1": hashlib.sha1(result.text.encode("utf-8"), usedforsecurity=False).hexdigest(),
        }
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return result


def validate_grep_options(options: str) -> list[str]:
    """Split ``options`` and keep only the grep flags an answering agent may use.

    Options that read a file (``-f``), change the file set (``--include``), or
    pipe through another program are refused. ``-r`` is accepted and implied:
    the wrapper searches every file of the snapshot in sorted order, and ``-H``
    is implied, so a one-file snapshot still prints file names as ``grep -r`` does.
    """

    try:
        tokens = shlex.split(options)
    except ValueError as exc:
        raise GrepOptionError("the grep options do not parse") from exc
    accepted: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _GREP_COUNT_OPTIONS:
            index += 1
            if index >= len(tokens) or not tokens[index].isdigit():
                raise GrepOptionError(f"{token} needs a number")
            accepted.extend([token, tokens[index]])
        elif re.fullmatch(r"-[ABCm]\d+", token):
            accepted.append(token)
        elif re.fullmatch(r"-[A-Za-z]+", token) and all(letter in _GREP_LETTERS for letter in token[1:]):
            accepted.append(token)
        else:
            raise GrepOptionError(f"grep option {token!r} is not allowed")
        index += 1
    return accepted


def cut_to_cap(data: bytes, cap: int | None) -> tuple[str, bool]:
    """The grep output an agent sees: cut at ``cap`` bytes, with the cut stated."""

    if cap is None or len(data) <= cap:
        return data.decode("utf-8", errors="replace"), False
    cut = data[:cap].decode("utf-8", errors="ignore")
    return cut + f"\n[output cut at {cap} bytes]\n", True


def grep_search(snapshot_dir: Path, *, pattern: str, options: str, cap: int | None) -> SearchResult:
    grep = shutil.which("grep")
    if grep is None:
        raise BenchError("grep is not installed")
    flags = validate_grep_options(options)
    files = sorted(
        path.relative_to(snapshot_dir).as_posix() for path in snapshot_dir.rglob("*") if path.is_file()
    )
    if not files:
        raise BenchError("the snapshot holds no files")
    env = {name: value for name, value in os.environ.items() if name not in {"GREP_OPTIONS", "GREP_COLOR", "GREP_COLORS"}}
    try:
        completed = subprocess.run(
            [grep, "-H", *flags, "-e", pattern, "--", *files],
            cwd=snapshot_dir,
            capture_output=True,
            check=False,
            env=env,
            timeout=GREP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchError("grep timed out") from exc
    if completed.returncode not in (0, 1):
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BenchError("grep failed: " + message[:200])
    text, truncated = cut_to_cap(completed.stdout, cap)
    return SearchResult(text=text, status="ok", truncated=truncated, raw_bytes=len(completed.stdout))


# --------------------------------------------------------------------------
# Command line


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, help="Directory this harness owns. Vault, snapshot and manifest live here.")
    parser.add_argument("--data-dir", default=None, help="Vault directory. Must be inside the run directory.")
    parser.add_argument("--checkout", default=None, help="Repository whose apps/api/src is put first on the path.")
    parser.add_argument("--gates", default=None, help="Path of gates.json. Defaults to the one in the repository.")
    parser.add_argument(
        "--search-quality",
        default=None,
        choices=("off", "passage", "on"),
        help="Set ALICE_SEARCH_QUALITY for the run. Unset by default.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alice_bench", description=__doc__.split("\n\n", 1)[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Import a corpus folder into a fresh vault.")
    _add_run_arguments(build)
    build.add_argument("--corpus", required=True, help="Folder of Markdown files.")
    build.add_argument("--order", default="sorted", help="sorted, reverse or shuffle:SEED.")
    build.add_argument("--domain", default="project")
    build.add_argument("--sensitivity", default="internal")
    build.add_argument("--questions", default=None, help="Question set whose anchors are checked after the build.")
    build.add_argument("--rebuild", action="store_true", help="Replace an earlier build in the run directory.")
    build.add_argument("--strict-flags", action="store_true", help="Fail when an anchor is flagged and not approved.")

    anchors = sub.add_parser("anchors", help="Check anchors against the snapshot of a built vault.")
    _add_run_arguments(anchors)
    anchors.add_argument("--questions", required=True)
    anchors.add_argument("--strict-flags", action="store_true")

    recall = sub.add_parser("recall", help="Print the recall text for one query.")
    _add_run_arguments(recall)
    recall.add_argument("--query", required=True)

    batch = sub.add_parser("batch", help="Run a question set, both variants, and save the outputs.")
    _add_run_arguments(batch)
    batch.add_argument("--questions", required=True)
    batch.add_argument("--out", required=True)
    batch.add_argument("--answerer-model", default=None)
    batch.add_argument("--judge-model", default=None)

    search = sub.add_parser("search", help="The budgeted command an answering agent runs.")
    _add_run_arguments(search)
    search.add_argument("--arm", required=True, choices=("alice", "grep", "grep-uncapped"))
    search.add_argument("--state-dir", default=None, help="Counter and log of one answering run. Inside the run directory.")
    search.add_argument("--budget", type=int, default=None, help="Searches allowed. Defaults to gates.json.")
    search.add_argument("--query", default=None, help="Alice arm: the query.")
    search.add_argument("--pattern", default=None, help="Grep arms: the pattern.")
    search.add_argument(
        "--grep-options",
        default="",
        help="Grep arms: a few flags, written with an equals sign because they start with a dash, as in --grep-options='-i -n -C 2'.",
    )
    search.add_argument("--limit", type=int, default=None, help="Refused unless it equals the default.")
    search.add_argument("--context-depth", default=None, help="Refused unless it equals the default.")
    search.add_argument("--include-sources", default=None, help="Refused unless it equals the default.")

    score = sub.add_parser("score", help="Tier 1 over saved outputs.")
    score.add_argument("--questions", required=True)
    score.add_argument("--outputs", required=True, nargs="+", help="One outputs file per import order.")
    score.add_argument("--gates", default=None)
    score.add_argument("--json", action="store_true", help="Print the report as JSON.")
    score.add_argument("--per-question", action="store_true", help="Print a line per question. Dev sets only.")

    finger = sub.add_parser("fingerprint", help="Print the fingerprint of a run.")
    _add_run_arguments(finger)
    finger.add_argument("--questions", default=None)
    return parser


def _context(args: argparse.Namespace, *, create: bool = False) -> tuple[Path, Path, Path, Gates]:
    run_dir = prepare_run_dir(Path(args.run_dir), create=True) if create else require_run_dir(Path(args.run_dir))
    data_dir = check_vault_location(run_dir, run_dir / VAULT_DIRNAME if args.data_dir is None else Path(args.data_dir))
    repo = Path(args.checkout) if args.checkout else REPO_ROOT
    gates = load_gates(Path(args.gates) if args.gates else None)
    activate_checkout(repo)
    if not create:
        manifest = require_matching_build(_read_manifest(run_dir), repo)
        require_matching_vault(manifest, run_dir, data_dir)
    return run_dir, data_dir, repo, gates


def _read_manifest(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / MANIFEST_FILENAME
    if not path.is_file():
        return None
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _print_anchor_report(report: Mapping[str, Any], *, strict: bool) -> int:
    print(
        f"anchors: {report['anchors']} verbatim, {report['flagged']} flagged, "
        f"{report['flagged_unapproved']} flagged and not approved"
    )
    for row in report["rows"]:
        if row["flags"] and not row["approved"]:
            print(
                f"  flagged {row['question']} fact {row['fact']}: {','.join(row['flags'])} "
                f"(length {row['length']}, files {row['files']})"
            )
    if strict and report["flagged_unapproved"]:
        return EXIT_REFUSED
    return EXIT_OK


def _cmd_build(args: argparse.Namespace) -> int:
    run_dir, data_dir, repo, gates = _context(args, create=True)
    manifest = build_vault(
        corpus_dir=Path(args.corpus),
        run_dir=run_dir,
        data_dir=data_dir,
        order=args.order,
        domain=args.domain,
        sensitivity=args.sensitivity,
        rebuild=args.rebuild,
        repo=repo,
        search_quality=args.search_quality,
    )
    print(
        f"built {manifest['sources']} sources, {manifest['chunks']} chunks, order {manifest['order']}, "
        f"{len(manifest['withheld'])} files with withheld lines"
    )
    if args.questions:
        qset = load_questions(Path(args.questions))
        report = verify_anchors(qset, read_snapshot(run_dir / SNAPSHOT_DIRNAME), gates)
        return _print_anchor_report(report, strict=args.strict_flags)
    return EXIT_OK


def _cmd_anchors(args: argparse.Namespace) -> int:
    run_dir, _data_dir, _repo, gates = _context(args)
    qset = load_questions(Path(args.questions))
    report = verify_anchors(qset, read_snapshot(run_dir / SNAPSHOT_DIRNAME), gates)
    return _print_anchor_report(report, strict=args.strict_flags)


def _cmd_recall(args: argparse.Namespace) -> int:
    _run_dir, data_dir, _repo, _gates = _context(args)
    sys.stdout.write(McpSession(vault_path(data_dir)).recall(args.query))
    sys.stdout.write("\n")
    return EXIT_OK


def _cmd_batch(args: argparse.Namespace) -> int:
    run_dir, data_dir, repo, gates = _context(args)
    qset = load_questions(Path(args.questions))
    db_path = vault_path(data_dir)
    session = McpSession(db_path)
    before = vault_row_counts(db_path)
    outputs: dict[str, dict[str, str]] = {}
    for question in qset.questions:
        outputs[question.id] = {variant: session.recall(question.query_for(variant)) for variant in VARIANTS}
    after = vault_row_counts(db_path)
    if before != after:
        raise BenchError("a recall changed rows in the vault: " + ", ".join(sorted(k for k in before if before[k] != after.get(k))))
    document = {
        "schema": OUTPUTS_SCHEMA,
        "question_set_sha256": qset.sha256,
        "fingerprint": fingerprint(
            repo=repo,
            gates=gates,
            session=session,
            manifest=_read_manifest(run_dir),
            search_quality=args.search_quality,
            question_set_sha256=qset.sha256,
            answerer_model=args.answerer_model,
            judge_model=args.judge_model,
        ),
        "outputs": outputs,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(outputs)} questions, {len(VARIANTS)} variants, to {out_path.name}")
    return EXIT_OK


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    run_dir, data_dir, repo, gates = _context(args)
    qsha = load_questions(Path(args.questions)).sha256 if args.questions else None
    print(
        json.dumps(
            fingerprint(
                repo=repo,
                gates=gates,
                session=McpSession(vault_path(data_dir)),
                manifest=_read_manifest(run_dir),
                search_quality=args.search_quality,
                question_set_sha256=qsha,
            ),
            indent=1,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _parse_bool_option(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise PinnedSettingError("--include-sources takes true or false")


def _cmd_search(args: argparse.Namespace) -> int:
    run_dir, data_dir, _repo, gates = _context(args)
    state_dir = resolve_inside(run_dir, args.state_dir, default=run_dir / STATE_DIRNAME / "default", what="--state-dir")
    budget = gates.searches_per_run if args.budget is None else args.budget
    if args.arm == "alice":
        if args.query is None:
            raise BenchError("the alice arm needs --query")
        arguments = alice_arm_arguments(
            args.query,
            limit=args.limit,
            context_depth=args.context_depth,
            include_sources=_parse_bool_option(args.include_sources),
        )
        session = McpSession(vault_path(data_dir))

        def runner() -> SearchResult:
            is_error, text = session.call(RECALL_TOOL, arguments)
            return SearchResult(text=text, status="error" if is_error else "ok", truncated=False, raw_bytes=len(text.encode("utf-8")))

        search_input = args.query
    else:
        if args.pattern is None:
            raise BenchError("the grep arms need --pattern")
        cap = gates.grep_cap if args.arm == "grep" else None
        snapshot_dir = run_dir / SNAPSHOT_DIRNAME
        pattern, options = args.pattern, args.grep_options
        validate_grep_options(options)

        def runner() -> SearchResult:
            return grep_search(snapshot_dir, pattern=pattern, options=options, cap=cap)

        search_input = json.dumps({"pattern": pattern, "options": options}, sort_keys=True)
    result = run_budgeted_search(state_dir, arm=args.arm, search_input=search_input, budget=budget, runner=runner)
    sys.stdout.write(result.text)
    if not result.text.endswith("\n"):
        sys.stdout.write("\n")
    return EXIT_OK if result.status == "ok" else EXIT_SEARCH_ERROR


def _cmd_score(args: argparse.Namespace) -> int:
    gates = load_gates(Path(args.gates) if args.gates else None)
    qset = load_questions(Path(args.questions))
    documents: list[tuple[str, dict[str, Any]]] = []
    for path_text in args.outputs:
        path = Path(path_text)
        documents.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    require_comparable_outputs(documents)
    reports: list[tuple[str, dict[str, Any]]] = []
    for name, document in documents:
        fp = document.get("fingerprint") or {}
        label = str(fp.get("import_order") or Path(name).stem)
        reports.append((label, score_outputs_document(document, qset, gates)))
    minimums = minimum_across_orders(reports, gates)
    if args.json:
        print(
            json.dumps(
                {
                    "orders": {
                        label: (report if args.per_question else without_per_question(report))
                        for label, report in reports
                    },
                    "minimum_hits": minimums,
                    "gates_sha256": gates.sha256,
                },
                indent=1,
                sort_keys=True,
            )
        )
        return EXIT_OK
    for label, report in reports:
        print(format_report(f"order {label}", report, gates, per_question=args.per_question))
        print()
    if len(reports) > 1:
        print("minimum hits over the import orders")
        for variant, by_budget in minimums.items():
            print("  " + variant + ": " + ", ".join(f"{budget} bytes {value}" for budget, value in by_budget.items()))
    print(f"gates.json sha256 {gates.sha256}")
    return EXIT_OK


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "build": _cmd_build,
    "anchors": _cmd_anchors,
    "recall": _cmd_recall,
    "batch": _cmd_batch,
    "search": _cmd_search,
    "score": _cmd_score,
    "fingerprint": _cmd_fingerprint,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    allow = arm_environment(getattr(args, "search_quality", None))
    try:
        with scoped_environment(allow):
            return _COMMANDS[args.command](args)
    except BudgetExhausted as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except BenchError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
