"""Per-project memory, slice S1: the resolver (spec tests 1 to 3, 6 to 11).

The resolver turns a start folder into a project by reading files. These tests
build real git layouts on disk (by hand, and with the real ``git`` binary under
a scrubbed environment when it is installed) and use an in-memory filesystem for
the Windows and case-insensitive rules. Each docstring names the mutation that
must fail the test.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from alicebot_api.project_identity import (
    MAX_CONFIG_BYTES,
    MAX_GIT_FILE_BYTES,
    MAX_WALK_LEVELS,
    Detection,
    OsFileSystem,
    Platform,
    detect_project,
    host_platform,
    is_absolute_path,
    parse_git_config,
    project_id_for,
    resolve_project,
)
from tests.unit.project_identity_support import (
    FakeFileSystem,
    config_text,
    make_repo,
    run_git,
)

REMOTE = "https://git.example.com/org/payments.git"
REMOTE_NORMALIZED = "git.example.com/org/payments"


def resolve(
    start: Path | str,
    *,
    home: Path | str | None,
    fs: object | None = None,
    windows: bool = False,
    source: str = "cwd",
) -> Detection:
    return resolve_project(
        str(start),
        start_source=source,  # type: ignore[arg-type]
        platform=Platform(windows=windows, home=None if home is None else str(home)),
        fs=fs or OsFileSystem(),  # type: ignore[arg-type]
    )


class SpyFileSystem(OsFileSystem):
    """The real filesystem, recording every probe and read."""

    def __init__(self) -> None:
        self.probed: list[str] = []
        self.reads: list[tuple[str, int]] = []

    def entry_kind(self, path: str):  # type: ignore[no-untyped-def]
        self.probed.append(path)
        return super().entry_kind(path)

    def read_capped(self, path: str, limit: int) -> bytes:
        self.reads.append((path, limit))
        return super().read_capped(path, limit)


def path_id(path: Path | str, *, fold: bool = False) -> str:
    text = str(path)
    return project_id_for("path", text.lower() if fold else text)


# ---------------------------------------------------------------------------
# Test 1
# ---------------------------------------------------------------------------


def test_symlink_into_a_subfolder_finds_the_repository(tmp_path: Path) -> None:
    """A symlink into a repository subfolder resolves to the repository.

    Mutation: walk the lexical path instead of the real one. The walk from the
    link name goes up the link's own parents, never meets the repository, and
    the second assertion fails.
    """

    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    repo = make_repo(root / "work" / "repo", config=config_text(REMOTE))
    (repo / "sub" / "deeper").mkdir(parents=True)
    link = root / "link"
    link.symlink_to(repo / "sub" / "deeper")

    direct = resolve(repo / "sub" / "deeper", home=home)
    via_link = resolve(link, home=home)

    assert direct.outcome == "found"
    assert via_link.outcome == "found"
    assert via_link.context is not None and direct.context is not None
    assert via_link.context.ids == direct.context.ids
    assert via_link.context.ids[1] == path_id(repo / ".git")


def test_the_home_folder_is_compared_as_a_real_path_and_its_git_is_not_read(tmp_path: Path) -> None:
    """A repository at the home folder is no project, and its ``.git`` is not read.

    The home folder is given through a symlink, so only a comparison of real
    paths recognizes it.

    Mutation: drop the home exclusion, or compare the home folder lexically.
    The walk then finds the dotfiles repository and returns ``found``, and the
    probe list shows its ``.git`` was read.
    """

    root = tmp_path.resolve()
    real_home = root / "realhome"
    make_repo(real_home, config=config_text("https://git.example.com/me/dotfiles.git"))
    (real_home / "work").mkdir()
    home_link = root / "homelink"
    home_link.symlink_to(real_home)
    spy = SpyFileSystem()

    detection = resolve(real_home / "work", home=home_link, fs=spy)

    assert detection.outcome == "none"
    assert detection.reason == "reached_home_folder"
    git_dir = str(real_home / ".git")
    assert not [path for path in spy.probed if path.startswith(git_dir)]
    assert not [path for path, _limit in spy.reads if path.startswith(git_dir)]


def test_the_home_folder_itself_is_no_project_and_a_repository_below_it_is(tmp_path: Path) -> None:
    """Home is no project, a repository below it resolves to the nearer one.

    Mutation: stop the walk one level late (the home folder's ``.git`` is read
    when the start folder is the home folder), or let a home repository hide a
    nested one.
    """

    root = tmp_path.resolve()
    home = make_repo(root / "home", config=config_text("https://git.example.com/me/dotfiles.git"))
    nested = make_repo(home / "code" / "app", config=config_text(REMOTE))

    at_home = resolve(home, home=home)
    below = resolve(nested, home=home)

    assert at_home.outcome == "none" and at_home.reason == "reached_home_folder"
    assert below.outcome == "found"
    assert below.context is not None
    assert below.context.ids[0] == project_id_for("remote", REMOTE_NORMALIZED)


def test_an_unknown_home_folder_is_stopped_only_by_the_root(tmp_path: Path) -> None:
    """With no home folder known, only the filesystem root ends the walk.

    Mutation: treat an unknown home as the working folder or as ``/``, which
    would stop the walk before the repository.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "dotfiles", config=config_text(REMOTE))
    (repo / "work").mkdir()

    assert resolve(repo / "work", home=None).outcome == "found"
    assert resolve(repo / "work", home="relative/home").outcome == "found"
    fs = FakeFileSystem()
    fs.add_dir("/empty/deeper")
    nowhere = resolve_project(
        "/empty/deeper", start_source="cwd", platform=Platform(windows=False, home=None), fs=fs
    )
    assert nowhere.outcome == "none" and nowhere.reason == "reached_filesystem_root"


# ---------------------------------------------------------------------------
# Test 2
# ---------------------------------------------------------------------------


def test_the_walk_stops_at_32_levels(tmp_path: Path) -> None:
    """The walk looks in at most 32 folders, the start folder counted.

    A repository 31 levels above the start folder is found and one 32 levels
    above is not. On a 100-level tree the walk probes at most 32 ``.git``
    entries.

    Mutation: drop the depth cap. The 100-level tree then finds the repository
    at its top.
    """

    assert MAX_WALK_LEVELS == 32
    root = tmp_path.resolve()
    repo = make_repo(root / "top", config=config_text(REMOTE))

    def below(levels: int) -> Path:
        folder = repo
        for _ in range(levels):
            folder = folder / "d"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    spy = SpyFileSystem()
    assert resolve(below(31), home=None).outcome == "found"
    far = resolve(below(32), home=None)
    assert far.outcome == "none" and far.reason == "walk_limit_reached"
    deep = resolve(below(100), home=None, fs=spy)
    assert deep.outcome == "none" and deep.reason == "walk_limit_reached"
    assert len([path for path in spy.probed if path.endswith(os.sep + ".git")]) <= MAX_WALK_LEVELS


# ---------------------------------------------------------------------------
# Test 3
# ---------------------------------------------------------------------------

REMOTE_CASES: list[tuple[str, str, str | None]] = [
    (
        "origin wins over another remote listed first",
        config_text("https://git.example.com/org/upstream.git", name="upstream")
        + '[remote "origin"]\n\turl = https://git.example.com/org/mine.git\n',
        "git.example.com/org/mine",
    ),
    (
        "the only remote with a URL, when it is not origin",
        config_text("https://git.example.com/org/solo.git", name="upstream"),
        "git.example.com/org/solo",
    ),
    (
        "two remotes and none is origin",
        config_text("https://git.example.com/org/a.git", name="one")
        + '[remote "two"]\n\turl = https://git.example.com/org/b.git\n',
        None,
    ),
    (
        "origin has no URL and another remote has one",
        '[remote "origin"]\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        '[remote "mirror"]\n\turl = https://git.example.com/org/mirror.git\n',
        "git.example.com/org/mirror",
    ),
    (
        "origin is a local path, so the chosen remote names this machine",
        config_text("/srv/git/foo.git")
        + '[remote "upstream"]\n\turl = https://git.example.com/org/real.git\n',
        None,
    ),
    (
        "origin is a file URL and another remote has a real URL",
        config_text("file:///srv/git/foo.git")
        + '[remote "upstream"]\n\turl = https://git.example.com/org/real.git\n',
        None,
    ),
    (
        "origin is a local path and the real remotes are listed before it",
        '[remote "upstream"]\n\turl = https://git.example.com/org/one.git\n'
        '[remote "mirror"]\n\turl = https://git.example.com/org/two.git\n'
        '[remote "origin"]\n\turl = ../elsewhere\n',
        None,
    ),
    (
        "origin is a Windows drive path and the one other remote is listed first",
        '[remote "upstream"]\n\turl = https://git.example.com/org/real.git\n'
        '[remote "origin"]\n\turl = C:\\\\work\\\\foo\n',
        None,
    ),
    (
        "a host that is not case folded keeps the case of the path in the id",
        config_text("https://Git.Example.COM/Org/Payments.git"),
        "git.example.com/Org/Payments",
    ),
    ("a local path", config_text("/srv/git/foo.git"), None),
    ("a file URL", config_text("file:///srv/git/foo.git"), None),
    ("a Windows drive path (git writes the backslashes doubled)", config_text("C:\\\\work\\\\foo"), None),
    ("a relative path", config_text("../foo"), None),
    ("no remote at all", config_text(), None),
]


@pytest.mark.parametrize(("label", "config", "normalized"), REMOTE_CASES, ids=[case[0] for case in REMOTE_CASES])
def test_remote_choice(tmp_path: Path, label: str, config: str, normalized: str | None) -> None:
    """The remote is origin, else the only remote with a URL, else none.

    A URL with no host counts as none.

    Mutation: take the first remote in the file (the first case), or accept a
    local path as a remote (the local path cases).

    Two more, each alone. Fall through to another remote when origin is chosen
    and its URL has no host (the three origin cases), which would give a
    repository that fetches from a local path the id of its upstream. Lowercase
    the normalized URL before it is hashed (the case-kept host), which would
    merge ``Org/Payments`` with ``org/payments`` on a host where they differ.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "repo", config=config)

    detection = resolve(repo, home=root / "home")

    assert detection.outcome == "found", label
    context = detection.context
    assert context is not None
    if normalized is None:
        assert context.source == "repo_path"
        assert context.ids == (path_id(repo / ".git"),)
    else:
        assert context.source == "remote"
        assert context.ids == (project_id_for("remote", normalized), path_id(repo / ".git"))


def test_insteadof_rewrites_are_not_applied(tmp_path: Path) -> None:
    """An ``insteadOf`` alias is hashed as written, not as the URL it expands to.

    Mutation: apply ``url.<base>.insteadOf``, which would turn ``gh:acme/payments``
    into ``github.com/acme/payments``.
    """

    root = tmp_path.resolve()
    config = config_text("gh:acme/payments") + '[url "git@github.com:"]\n\tinsteadOf = gh:\n'
    repo = make_repo(root / "repo", config=config)

    context = resolve(repo, home=root / "home").context

    assert context is not None
    assert context.ids[0] == project_id_for("remote", "gh/acme/payments")
    assert context.ids[0] != project_id_for("remote", "github.com/acme/payments")


# ---------------------------------------------------------------------------
# Test 6
# ---------------------------------------------------------------------------


def _write_worktree(main_git: Path, worktree: Path, name: str = "wt") -> None:
    """The layout ``git worktree add`` writes: a ``.git`` file and a ``commondir``."""

    admin = main_git / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/wt\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {admin}\n")


@pytest.mark.parametrize("remote", [REMOTE, None], ids=["with a remote", "no remote"])
def test_a_linked_worktree_gets_the_main_checkouts_ids(tmp_path: Path, remote: str | None) -> None:
    """A linked worktree agrees with its main checkout, with and without a remote.

    Mutation: hash the parent of the common directory, or the worktree's own
    folder, which makes the two checkouts differ.
    """

    root = tmp_path.resolve()
    main = make_repo(root / "main", config=config_text(remote))
    _write_worktree(main / ".git", root / "elsewhere" / "wt")

    from_main = resolve(main, home=root / "home").context
    from_worktree = resolve(root / "elsewhere" / "wt", home=root / "home").context

    assert from_main is not None and from_worktree is not None
    assert from_worktree.ids == from_main.ids
    assert from_main.ids[-1] == path_id(main / ".git")
    assert from_worktree.label == from_main.label == ("payments" if remote else "main")


def test_a_submodule_is_its_own_project(tmp_path: Path) -> None:
    """A submodule (a ``.git`` file into the superproject's ``modules`` folder,
    no ``commondir``) has ids of its own, different from the superproject's.

    Mutation: follow the ``.git`` file up to the superproject, or read
    ``commondir`` as if the superproject were the common directory.
    """

    root = tmp_path.resolve()
    super_repo = make_repo(root / "super", config=config_text("https://git.example.com/org/super.git"))
    module_dir = super_repo / ".git" / "modules" / "sub"
    module_dir.mkdir(parents=True)
    (module_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (module_dir / "config").write_text(config_text("https://git.example.com/org/sub.git"))
    sub = super_repo / "vendor" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/sub\n")

    outer = resolve(super_repo, home=root / "home").context
    inner = resolve(sub, home=root / "home").context

    assert outer is not None and inner is not None
    assert inner.ids[0] == project_id_for("remote", "git.example.com/org/sub")
    assert inner.ids[1] == path_id(module_dir)
    assert not set(inner.ids) & set(outer.ids)
    assert inner.label == "sub"


def test_bare_repository_with_a_worktree_uses_the_bare_path_and_bare_repositories_differ(
    tmp_path: Path,
) -> None:
    """A bare repository with a linked worktree is identified by its own path, two
    bare repositories in one folder differ, and a folder inside a bare repository
    is no project.

    Mutation: hash the parent of the common directory. Both bare repositories
    would share the folder around them and the exact ids below would not match.
    """

    root = tmp_path.resolve()
    holder = root / "repos"
    ids: list[tuple[str, ...]] = []
    for name in ("first.git", "second.git"):
        bare = holder / name
        bare.mkdir(parents=True)
        (bare / "HEAD").write_text("ref: refs/heads/main\n")
        (bare / "config").write_text(config_text().replace("bare = false", "bare = true"))
        worktree = root / "trees" / name.removesuffix(".git")
        _write_worktree(bare, worktree)
        context = resolve(worktree, home=root / "home").context
        assert context is not None
        assert context.ids == (path_id(bare),)
        assert context.label == name.removesuffix(".git")
        ids.append(context.ids)
        assert resolve(bare, home=root / "home").outcome == "none"
        (bare / "refs").mkdir()
        assert resolve(bare / "refs", home=root / "home").outcome == "none"
    assert ids[0] != ids[1]


def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.mark.skipif(not _git_available(), reason="git is not installed")
def test_layouts_built_by_real_git_resolve_as_the_spec_says(tmp_path: Path) -> None:
    """The same layouts, written by real git 's own commands.

    A normal repository, a subfolder, a linked worktree, a submodule, and a bare
    clone with a linked worktree.

    Mutation: any change that breaks one of the hand-built layouts above, since
    this test checks that the hand-built files match what git writes.
    """

    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    work = root / "work"
    work.mkdir()
    repo = work / "repo"
    run_git(home, work, "init", "-q", "repo")
    run_git(home, repo, "remote", "add", "origin", "git@GitHub.com:Acme/Payments.git")
    run_git(home, repo, "commit", "-q", "--allow-empty", "-m", "first")
    (repo / "sub" / "dir").mkdir(parents=True)

    def real_path_id(git_dir: Path) -> str:
        # git writes ignorecase = true on a case-insensitive volume (a default Mac),
        # and the path hash folds the path exactly then.
        config = (git_dir / "config").read_text() if (git_dir / "config").exists() else ""
        return path_id(git_dir, fold="ignorecase = true" in config)

    expected = (project_id_for("remote", "github.com/acme/payments"), real_path_id(repo / ".git"))

    assert resolve(repo, home=home).context.ids == expected  # type: ignore[union-attr]
    assert resolve(repo / "sub" / "dir", home=home).context.ids == expected  # type: ignore[union-attr]

    run_git(home, repo, "worktree", "add", "-q", str(work / "wt"))
    assert resolve(work / "wt", home=home).context.ids == expected  # type: ignore[union-attr]

    other = work / "other"
    run_git(home, work, "init", "-q", "other")
    run_git(home, other, "commit", "-q", "--allow-empty", "-m", "first")
    run_git(
        home, repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(other), "vendor/other"
    )
    module_dir = repo / ".git" / "modules" / "vendor" / "other"
    inner = resolve(repo / "vendor" / "other", home=home).context
    assert inner is not None and not set(inner.ids) & set(expected)
    assert inner.ids == (real_path_id(module_dir),)

    bare = work / "bare.git"
    run_git(home, work, "clone", "-q", "--bare", str(other), "bare.git")
    run_git(home, bare, "worktree", "add", "-q", str(work / "bare_wt"))
    context = resolve(work / "bare_wt", home=home).context
    assert context is not None and context.ids == (real_path_id(bare),)
    assert resolve(bare, home=home).outcome == "none"


# ---------------------------------------------------------------------------
# Test 7: the id algebra that the two-session sequences rely on
# ---------------------------------------------------------------------------


def _visible(note_project: str, current_ids: tuple[str, ...]) -> bool:
    """The only rule the project view applies to a project id: a note is in the
    view when its id is one of the current ids. The view itself lands with S2."""

    return note_project in current_ids


def test_two_session_sequences_keep_notes_reachable_by_id(tmp_path: Path) -> None:
    """Notes written under a primary id stay reachable in the cases the spec lists.

    The notes and the view land with S2 and S3, which extend this test with
    recall and ``scope: "all"``. Here a note is the id it was written under, and
    the view is membership in the current ids.

    Mutation: drop the secondary id, derive the primary id from the path when a
    remote exists, or drop the port before hashing.
    """

    root = tmp_path.resolve()
    home = root / "home"

    # A repository with no remote, then given a remote: the old note stays visible.
    first = make_repo(root / "first", config=config_text())
    before = resolve(first, home=home).context
    assert before is not None
    note_before_remote = before.ids[0]
    (first / ".git" / "config").write_text(config_text(REMOTE))
    after = resolve(first, home=home).context
    assert after is not None
    assert after.source == "remote" and after.ids[0] != note_before_remote
    assert _visible(note_before_remote, after.ids)
    note_after_remote = after.ids[0]

    # A second clone at another path shares the primary id, and sees the note.
    clone = make_repo(root / "clone", config=config_text(REMOTE))
    cloned = resolve(clone, home=home).context
    assert cloned is not None
    assert cloned.ids[0] == after.ids[0]
    assert _visible(note_after_remote, cloned.ids)
    assert not _visible(note_before_remote, cloned.ids)  # written under the first clone's path id

    # A no-remote repository that moves gets a new id and shares no id.
    lonely = make_repo(root / "lonely", config=config_text())
    lonely_before = resolve(lonely, home=home).context
    assert lonely_before is not None
    moved = root / "moved"
    lonely.rename(moved)
    lonely_after = resolve(moved, home=home).context
    assert lonely_after is not None
    assert not set(lonely_after.ids) & set(lonely_before.ids)
    assert not _visible(lonely_before.ids[0], lonely_after.ids)

    # Two repositories on one host at different ports are different projects.
    first_port = make_repo(root / "p1", config=config_text("https://h.example:8443/o/r"))
    second_port = make_repo(root / "p2", config=config_text("https://h.example:9443/o/r"))
    one = resolve(first_port, home=home).context
    two = resolve(second_port, home=home).context
    assert one is not None and two is not None
    assert one.ids[0] != two.ids[0]
    assert not set(one.ids) & set(two.ids)

    # One repository reached by ssh on 2222 and by https on 443 is two projects.
    by_ssh = make_repo(root / "ssh", config=config_text("ssh://git@h.example:2222/o/r"))
    by_https = make_repo(root / "https", config=config_text("https://h.example/o/r"))
    ssh_context = resolve(by_ssh, home=home).context
    https_context = resolve(by_https, home=home).context
    assert ssh_context is not None and https_context is not None
    assert ssh_context.ids[0] != https_context.ids[0]
    assert ssh_context.ids[0] == project_id_for("remote", "h.example:2222/o/r")
    assert https_context.ids[0] == project_id_for("remote", "h.example/o/r")


# ---------------------------------------------------------------------------
# Test 8
# ---------------------------------------------------------------------------


def _fake_repo(fs: FakeFileSystem, root: str, config: str, *, sep: str = "/") -> None:
    fs.add_dir(root)
    fs.add_file(f"{root}{sep}.git{sep}HEAD", "ref: refs/heads/main\n")
    fs.add_file(f"{root}{sep}.git{sep}config", config)


def test_case_rules_for_the_path_hash() -> None:
    """``ignorecase = true`` folds the path, a config without it keeps case, Windows folds.

    The platform is injected, so one runner covers all three.

    Mutation: fold always (the case-sensitive pair would then agree), or never
    (the ``ignorecase`` pair and the Windows pair would then differ).
    """

    posix = Platform(windows=False, home="/home/me")

    def ids(config: str) -> tuple[str, str]:
        fs = FakeFileSystem()
        _fake_repo(fs, "/Work/App", config)
        _fake_repo(fs, "/work/app", config)
        upper = resolve_project("/Work/App", start_source="cwd", platform=posix, fs=fs).context
        lower = resolve_project("/work/app", start_source="cwd", platform=posix, fs=fs).context
        assert upper is not None and lower is not None
        return upper.ids[0], lower.ids[0]

    keeps_case = ids(config_text())
    assert keeps_case[0] != keeps_case[1]
    assert keeps_case[0] == project_id_for("path", "/Work/App/.git")
    folded = ids(config_text(extra="[core]\n\tignorecase = true\n"))
    assert folded[0] == folded[1] == project_id_for("path", "/work/app/.git")
    # git also reads yes, on and a number as true, and a bare key as true.
    for spelling in ("yes", "ON", "1", "2"):
        again = ids(config_text(extra=f"[core]\n\tignorecase = {spelling}\n"))
        assert again[0] == again[1]
    bare_key = ids(config_text(extra="[core]\n\tignorecase\n"))
    assert bare_key[0] == bare_key[1]
    for spelling in ("false", "no", "0"):
        off = ids(config_text(extra=f"[core]\n\tignorecase = {spelling}\n"))
        assert off[0] != off[1]
    # The last assignment wins.
    last = ids(config_text(extra="[core]\n\tignorecase = true\n[core]\n\tignorecase = false\n"))
    assert last[0] != last[1]

    windows = Platform(windows=True, home="C:\\Users\\Me")
    fs = FakeFileSystem(windows=True)
    _fake_repo(fs, "C:\\Work\\App", config_text(), sep="\\")
    context = resolve_project("C:\\Work\\App", start_source="cwd", platform=windows, fs=fs).context
    assert context is not None
    assert context.ids[0] == project_id_for("path", "c:\\work\\app\\.git")


# ---------------------------------------------------------------------------
# Test 9
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "absolute"),
    [
        ("C:\\work\\repo", True),
        ("c:/work/repo", True),
        ("C:\\", True),
        ("C:/", True),
        ("\\\\server\\share\\repo", True),
        ("//server/share/repo", True),
        ("\\\\server\\share", True),
        ("\\work\\repo", False),
        ("/work/repo", False),
        ("C:work\\repo", False),
        ("C:", False),
        ("work\\repo", False),
        ("\\\\server", False),
        ("\\\\\\server\\share", False),
        ("\\\\?\\C:\\work", False),
        ("\\\\.\\C:\\work", False),
        ("", False),
        ("C:\\work\0", False),
    ],
)
def test_windows_absolute_paths(text: str, absolute: bool) -> None:
    """Drive-letter and UNC paths are absolute, a rooted path with no drive is not.

    Mutation: accept a rooted path with no drive (``\\work\\repo``), accept a
    bare drive (``C:``), or accept the device prefixes.
    """

    assert is_absolute_path(text, windows=True) is absolute


@pytest.mark.parametrize(
    ("text", "absolute"),
    [
        ("/work/repo", True),
        ("/mnt/c/Users/me/repo", True),
        ("C:\\work\\repo", False),
        ("c:/work/repo", False),
        ("work/repo", False),
        ("", False),
        ("/work\0", False),
    ],
)
def test_posix_absolute_paths_include_a_wsl_style_path_and_refuse_a_windows_path(
    text: str, absolute: bool
) -> None:
    """A ``/mnt/c/...`` path is a POSIX path, and a Windows path is not absolute on POSIX.

    Mutation: treat a drive-letter path as absolute on POSIX, or a ``/mnt/c``
    path as a Windows path.
    """

    assert is_absolute_path(text, windows=False) is absolute


def test_windows_resolution_rules() -> None:
    """Drive case and slash style give one id, the home folder (``USERPROFILE``)
    and a drive root are no project, and UNC paths resolve.

    Mutation: compare drive letters exactly (the ``c:`` spelling would miss the
    store), accept a rooted path with no drive, or read a repository at a drive
    root.
    """

    fs = FakeFileSystem(windows=True)
    _fake_repo(fs, "C:\\Work\\Repo", config_text(REMOTE), sep="\\")
    fs.add_dir("C:\\Work\\Repo\\sub")
    fs.add_dir("C:\\Users\\Me\\proj")
    _fake_repo(fs, "C:\\Users\\Me", config_text("https://git.example.com/me/dotfiles.git"), sep="\\")
    _fake_repo(fs, "C:\\", config_text(REMOTE), sep="")
    platform = host_platform({"USERPROFILE": "c:/users/me", "HOME": "/ignored"}, windows=True)
    assert platform == Platform(windows=True, home="c:/users/me")

    def found(start: str) -> tuple[str, ...]:
        detection = resolve_project(start, start_source="argument", platform=platform, fs=fs)
        assert detection.outcome == "found", (start, detection)
        assert detection.context is not None
        return detection.context.ids

    expected = (project_id_for("remote", REMOTE_NORMALIZED), project_id_for("path", "c:\\work\\repo\\.git"))
    assert found("C:\\Work\\Repo") == expected
    assert found("c:\\work\\repo") == expected
    assert found("C:/Work/Repo/sub") == expected
    assert found("c:/WORK/repo\\sub") == expected

    fs.reads.clear()
    fs.probed.clear()
    for blocked in ("C:\\Users\\Me", "c:/users/me", "C:\\"):
        detection = resolve_project(blocked, start_source="argument", platform=platform, fs=fs)
        assert detection.outcome == "none", blocked
    root_detection = resolve_project("C:\\", start_source="argument", platform=platform, fs=fs)
    assert root_detection.reason == "reached_filesystem_root"
    assert not [path for path in fs.probed if path.lower().startswith("c:\\users\\me\\.git")]
    assert not [path for path in fs.probed if path.lower() in ("c:\\.git", "c:.git")]
    below_home = resolve_project("C:\\Users\\Me\\proj", start_source="argument", platform=platform, fs=fs)
    assert below_home.outcome == "none" and below_home.reason == "reached_home_folder"

    plain = FakeFileSystem(windows=True, canonical_realpath=False)
    _fake_repo(plain, "C:\\Users\\Me", config_text("https://git.example.com/me/dotfiles.git"), sep="\\")
    plain.add_dir("C:\\Users\\Me\\proj")
    folded = resolve_project("C:\\Users\\Me\\proj", start_source="argument", platform=platform, fs=plain)
    assert folded.outcome == "none" and folded.reason == "reached_home_folder"
    assert not [path for path in plain.probed if path.lower().endswith("me\\.git")]

    rooted = resolve_project("\\Work\\Repo", start_source="argument", platform=platform, fs=fs)
    assert rooted.outcome == "none" and rooted.reason == "no_usable_start_folder"

    unc = FakeFileSystem(windows=True)
    _fake_repo(unc, "\\\\srv\\share\\repo", config_text(), sep="\\")
    unc_context = resolve_project(
        "//srv/share/repo", start_source="argument", platform=platform, fs=unc
    ).context
    assert unc_context is not None
    assert unc_context.ids == (project_id_for("path", "\\\\srv\\share\\repo\\.git"),)
    share_root = resolve_project("\\\\srv\\share\\", start_source="argument", platform=platform, fs=unc)
    assert share_root.outcome == "none"


def test_a_windows_path_handed_to_a_posix_process_is_ignored() -> None:
    """On POSIX a drive-letter path is not absolute, so the next source is tried.

    Mutation: accept a drive-letter path on POSIX.
    """

    posix = Platform(windows=False, home=None)
    fs = FakeFileSystem()
    fs.add_dir("/work")
    detection = resolve_project("C:\\work\\repo", start_source="argument", platform=posix, fs=fs)
    assert detection.outcome == "none" and detection.reason == "no_usable_start_folder"
    wsl = FakeFileSystem()
    _fake_repo(wsl, "/mnt/c/Users/me/repo", config_text(REMOTE))
    found = resolve_project("/mnt/c/Users/me/repo", start_source="argument", platform=posix, fs=wsl)
    assert found.outcome == "found"


# ---------------------------------------------------------------------------
# Test 10
# ---------------------------------------------------------------------------


class _RecordingEnvironment(dict):  # type: ignore[type-arg]
    """An environment that records which names are read."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.read: set[str] = set()

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().__contains__(key)


def test_the_resolver_starts_no_process_and_reads_no_environment_but_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution starts no process, and the only variable it reads is the home folder.

    ``subprocess``, ``os.system`` and ``os.popen`` raise for the whole test, and
    ``os.environ`` records every name read.

    Mutation: replace the file reads with ``subprocess`` (for example
    ``git config --get remote.origin.url``). The patched call raises, the
    resolver reports ``failed``, and the first assertion fails.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "repo", config=config_text(REMOTE))
    (repo / "sub").mkdir()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the resolver must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(os, "popen", refuse)
    environment = _RecordingEnvironment({"HOME": str(root / "home"), "PATH": "/usr/bin"})
    monkeypatch.setattr(os, "environ", environment)

    detection = detect_project(argument=str(repo / "sub"))

    assert detection.outcome == "found"
    assert environment.read <= {"HOME", "USERPROFILE"}
    assert "HOME" in environment.read


# ---------------------------------------------------------------------------
# Test 11
# ---------------------------------------------------------------------------


def _failed(detection: Detection, reason: str) -> None:
    assert detection.outcome == "failed", detection
    assert detection.context is None
    assert detection.reason == reason


def test_the_caps_give_failed_and_never_a_path_id(tmp_path: Path) -> None:
    """A ``.git`` file over 4 KiB, a config over 256 KiB, an include, a malformed
    ``gitdir:`` line, an unreadable config and a missing config each give no
    project and the outcome ``failed``, never a path id.

    Mutation: read past the caps (the one-byte-over files below would parse),
    fall back to the path id when the config is unreadable, or report a failure
    as ``none``.
    """

    assert MAX_GIT_FILE_BYTES == 4096
    assert MAX_CONFIG_BYTES == 262144
    root = tmp_path.resolve()
    home = root / "home"
    spy = SpyFileSystem()

    # A .git file over 4 KiB, and the largest one that is accepted.
    admin = root / "admin"
    make_repo(admin, config=config_text(REMOTE))
    large = root / "large"
    large.mkdir()
    pointer = f"gitdir: {admin / '.git'}\n"
    (large / ".git").write_text(pointer + "x" * (4097 - len(pointer)))
    _failed(resolve(large, home=home, fs=spy), "git_file_too_large")
    assert (str(large / ".git"), 4096) in spy.reads
    exact = root / "exact"
    exact.mkdir()
    (exact / ".git").write_text(pointer + "x" * (4096 - len(pointer)))
    assert resolve(exact, home=home).outcome == "found"

    # A config over 256 KiB, and the largest one that is accepted.
    padding = "# " + "p" * 100 + "\n"
    body = config_text(REMOTE)
    fits = body + padding * ((262144 - len(body)) // len(padding))
    fits += "#" * (262144 - len(fits))
    assert len(fits.encode()) == 262144
    exact_config = make_repo(root / "exact_config", config=fits)
    assert resolve(exact_config, home=home).outcome == "found"
    too_big = make_repo(root / "too_big", config=fits + "#")
    _failed(resolve(too_big, home=home, fs=spy), "config_too_large")
    assert (str(too_big / ".git" / "config"), 262144) in spy.reads

    # An include of any kind: an included file could hold the remote.
    for index, include in enumerate(("[include]\n\tpath = ../other.config\n", '[includeIf "gitdir:~/x/"]\n\tpath = y\n')):
        repo = make_repo(root / f"include{index}", config=config_text(REMOTE) + include)
        _failed(resolve(repo, home=home), "config_uses_include")

    # A malformed gitdir line, an empty one and a file that is not a pointer.
    for index, text in enumerate(("gitdir\n", "gitdir:\n", "gitdir:   \n", "not a pointer\n", "")):
        broken = root / f"broken{index}"
        broken.mkdir()
        (broken / ".git").write_text(text)
        _failed(resolve(broken, home=home), "git_file_malformed")

    # A pointer to a folder that is not there, and a commondir that is not there.
    dangling = root / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text(f"gitdir: {root / 'nowhere'}\n")
    _failed(resolve(dangling, home=home), "git_dir_unreadable")
    odd_admin = root / "odd_admin"
    odd_admin.mkdir()
    (odd_admin / "commondir").write_text("../nope\n")
    odd = root / "odd"
    odd.mkdir()
    (odd / ".git").write_text(f"gitdir: {odd_admin}\n")
    _failed(resolve(odd, home=home), "common_dir_unreadable")
    (odd_admin / "commondir").write_text("\n")
    _failed(resolve(odd, home=home), "common_dir_unreadable")

    # A config that is missing, a config that is a folder, a config with a syntax error.
    no_config = make_repo(root / "no_config", config=None)
    _failed(resolve(no_config, home=home), "config_missing_or_unreadable")
    folder_config = make_repo(root / "folder_config", config=None)
    (folder_config / ".git" / "config").mkdir()
    _failed(resolve(folder_config, home=home), "config_missing_or_unreadable")
    for index, text in enumerate(('[remote "origin\n\turl = x\n', "url = https://h/o/r\n", '[core]\n\tname = "unterminated\n')):
        bad = make_repo(root / f"syntax{index}", config=text)
        _failed(resolve(bad, home=home), "config_malformed")


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_config_gives_failed(tmp_path: Path) -> None:
    """A config the process may not read gives ``failed``, never a path id.

    Mutation: treat a read error as an empty config (no remote), which would
    return a path id.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "repo", config=config_text(REMOTE))
    config = repo / ".git" / "config"
    config.chmod(0)
    try:
        _failed(resolve(repo, home=root / "home"), "config_missing_or_unreadable")
    finally:
        config.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_pipe_in_place_of_a_config_or_a_git_file_is_refused_without_reading_it(tmp_path: Path) -> None:
    """A named pipe named ``config`` or ``.git`` must not block the resolver.

    Mutation: open the file before checking that it is a regular file. Reading
    a pipe with no writer blocks, the worker thread never finishes, and the
    join below times out.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "repo", config=None)
    os.mkfifo(repo / ".git" / "config")
    piped = root / "piped"
    piped.mkdir()
    os.mkfifo(piped / ".git")
    results: dict[str, Detection] = {}

    def work() -> None:
        results["config"] = resolve(repo, home=root / "home")
        results["git"] = resolve(piped, home=root / "home")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the resolver blocked on a named pipe"
    _failed(results["config"], "config_missing_or_unreadable")
    _failed(results["git"], "git_entry_unreadable")


def test_a_dangling_or_looping_git_link_gives_failed(tmp_path: Path) -> None:
    """A ``.git`` that is a symlink to nothing, or to itself, gives ``failed``.

    Mutation: treat a ``.git`` entry that cannot be statted as absent and keep
    walking up, which would find a repository above.
    """

    root = tmp_path.resolve()
    outer = make_repo(root / "outer", config=config_text(REMOTE))
    dangling = outer / "dangling"
    dangling.mkdir()
    (dangling / ".git").symlink_to(root / "missing-target")
    _failed(resolve(dangling, home=root / "home"), "git_entry_unreadable")
    looped = outer / "looped"
    looped.mkdir()
    (looped / ".git").symlink_to(looped / ".git")
    _failed(resolve(looped, home=root / "home"), "git_entry_unreadable")


class _RaisingFileSystem(OsFileSystem):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def read_capped(self, path: str, limit: int) -> bytes:
        raise self.error


def test_a_resolver_that_raises_is_caught_and_gives_failed(tmp_path: Path) -> None:
    """Any exception inside resolution is caught: the outcome is ``failed`` with
    the fixed reason, the message is not printed or logged, and nothing escapes.

    Mutation: let the exception escape, or report it as ``none``.
    """

    root = tmp_path.resolve()
    repo = make_repo(root / "repo", config=config_text(REMOTE))
    detection = resolve(repo, home=root / "home", fs=_RaisingFileSystem(RuntimeError(f"boom {repo}")))
    _failed(detection, "unexpected_error")
    edge = detect_project(
        argument=str(repo),
        platform=Platform(windows=False, home=None),
        fs=_RaisingFileSystem(RuntimeError("boom")),
    )
    _failed(edge, "unexpected_error")
    odd = detect_project(argument=str(repo), platform=object(), fs=OsFileSystem())  # type: ignore[arg-type]
    _failed(odd, "unexpected_error")
    permission = resolve(repo, home=root / "home", fs=_RaisingFileSystem(PermissionError(errno.EACCES, "denied")))
    _failed(permission, "config_missing_or_unreadable")


# ---------------------------------------------------------------------------
# Git config syntax: only what the resolver reads
# ---------------------------------------------------------------------------


def test_the_config_parser_reads_what_git_reads() -> None:
    """The parser reads quotes, comments, continuations, mixed-case names, a BOM,
    the legacy ``[remote.name]`` header, a key on the header line and duplicate
    ``url`` keys (the first wins), and nothing else.

    Mutation: drop comment handling (the commented-out ``url`` would win), keep
    trailing whitespace, take the last ``url`` key, or read ``pushurl``.
    """

    text = (
        "\ufeff# a comment\n"
        '[Remote "origin"]\n'
        "\tURL = https://git.example.com/org/first.git   # trailing comment\n"
        "\turl = https://git.example.com/org/second.git\n"
        "\tpushurl = https://git.example.com/org/push.git\n"
        "\t; url = https://git.example.com/org/commented.git\n"
        '[remote "quoted"]\n'
        '\turl = "https://git.example.com/org/qu oted.git"\n'
        "[remote.legacy]\n"
        "\turl = https://git.example.com/org/legacy.git\n"
        '[remote "continued"]\n'
        "\turl = https://git.example.com/org/\\\n"
        "con tinued.git\n"
        "[core] ignorecase = TRUE\n"
        '[remote "empty"]\n'
        "\turl =\n"
        "[branch \"main\"]\n"
        "\tremote = origin\n"
    )
    facts = parse_git_config(text.encode())
    assert dict(facts.remote_urls) == {
        "origin": "https://git.example.com/org/first.git",
        "quoted": "https://git.example.com/org/qu oted.git",
        "legacy": "https://git.example.com/org/legacy.git",
        "continued": "https://git.example.com/org/con tinued.git",
    }
    assert facts.ignorecase is True
    assert facts.has_include is False
    assert parse_git_config(b"").ignorecase is False
    assert parse_git_config(b"[Include]\n\tpath = x\n").has_include is True
    assert parse_git_config(b'[remote "a"]\n\turl = "a\\"b\\\\c"\n').remote_urls["a"] == 'a"b\\c'
    with pytest.raises(Exception):
        parse_git_config(b"[core]\n\tname = bad\\q\n")
    with pytest.raises(Exception):
        parse_git_config(b"[core\n")
    with pytest.raises(Exception):
        parse_git_config(b"[core]\n\tkey\x00 = 1\n")


def test_two_spellings_of_one_remote_resolve_to_one_primary_id(tmp_path: Path) -> None:
    """Two clones whose configs spell one remote differently get the same primary id.

    Mutation: hash the raw URL instead of the normalized one.
    """

    root = tmp_path.resolve()
    first = make_repo(root / "first", config=config_text("git@GitHub.com:Acme/Payments.git"))
    second = make_repo(root / "second", config=config_text("https://tok:x@github.com/acme/payments?x=1"))
    one = resolve(first, home=root / "home").context
    two = resolve(second, home=root / "home").context
    assert one is not None and two is not None
    assert one.ids[0] == two.ids[0] == project_id_for("remote", "github.com/acme/payments")
    assert one.ids[1] != two.ids[1]
