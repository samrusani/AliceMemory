"""Per-project memory S2: the SessionStart hook finds the project from the folder the host started in (spec 4.2, 6.4, tests 50 and 78).

Claude Code and Codex send the hook a ``cwd`` that is the folder the session started in (the host-evidence run
recorded it), and the resolver walks up from there to the git root. With scoping on the hook prints the project
line and the project's notes first. A detection that fails is not a failed hook: the brief carries a plain
status line and the hook exits 0. With scoping off, which is the default until the flip, the hook prints what
v0.20.0 printed (``test_per_project_s2_off_parity`` pins that).

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

import alicebot_api.project_view as project_view_module
from alicebot_api import session_start_hook
from alicebot_api.onramp import main as onramp_main
from alicebot_api.project_identity import MAX_HOOK_PAYLOAD_BYTES, detect_project
from alicebot_api.project_view import STATUS_LINE_FAILED, STATUS_LINE_NONE, STATUS_LINE_OFF
from alicebot_api.session_briefing import brief_char_len
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_loop, add_memory, context_for, db_path_for, repo_with_remote
from tests.unit.project_identity_support import config_text, make_repo

USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_MEMORY_DATA_DIR", "CLAUDE_PLUGIN_ROOT", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _repo(root: Path, remote: str) -> Path:
    return repo_with_remote(root, f"https://example.com/acme/{remote}.git")


def _ids(repo: Path) -> tuple[str, ...]:
    detection = detect_project(argument=str(repo))
    assert detection.context is not None
    return detection.context.ids


def _vault(tmp_path: Path, *repos: Path) -> Path:
    """A vault with one project fact per repository and one plain global fact."""

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.global", text="Plain global fact shared by all")
        for repo in repos:
            add_memory(store, key=f"fact.{repo.name}", text=f"Fact of project {repo.name} only", scope=(_ids(repo)[0],))
            add_loop(store, title=f"Loop of project {repo.name} only", scope=(_ids(repo)[0],))
    return data_dir


def _hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    payload: object = None,
    *args: str,
    fmt: str = "markdown",
    raw: bytes | None = None,
) -> str:
    body = raw if raw is not None else json.dumps(payload if payload is not None else {}).encode()
    stdin = io.TextIOWrapper(io.BytesIO(body), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    assert session_start_hook.main(["--format", fmt, "--data-dir", str(data_dir), *args]) == 0
    return capsys.readouterr().out


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")


def test_the_hook_finds_the_repository_from_a_subfolder_the_host_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: resolve the start folder with no walk up to the git root, or ignore the stdin ``cwd``.

    Claude Code and Codex start the hook in the launch folder, which can be a subfolder of the repository. The
    working folder of the hook process here is somewhere else entirely, so only the stdin ``cwd`` can find it.
    """

    repo = _repo(tmp_path / "payments", "payments")
    subfolder = repo / "src" / "service"
    subfolder.mkdir(parents=True)
    other = _repo(tmp_path / "search", "search")
    data_dir = _vault(tmp_path, repo, other)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(subfolder), "hook_event_name": "SessionStart"})
    lines = out.splitlines()
    assert lines[1].startswith('Alice project: "payments" (id ' + _ids(repo)[0])
    assert "found from the git remote" in lines[1]
    assert lines[2] == '**fact**: "Fact of project payments only"'
    assert '**fact** (global): "Plain global fact shared by all"' in out
    assert "project search" not in out
    json_out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(subfolder)}, fmt="json")
    assert "Alice project:" in json.loads(json_out)["additional_context"]


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("argument", "env", "stdin", "process"), "one"),
        (("env", "stdin", "process"), "two"),
        (("stdin", "process"), "three"),
        (("process",), "four"),
    ],
)
def test_the_start_folder_comes_from_the_first_source_that_has_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sources: tuple[str, ...],
    expected: str,
) -> None:
    """Mutation: reorder the sources: ``--project-dir``, ``ALICE_PROJECT_DIR``, the stdin ``cwd``, the working folder.

    Four repositories, one named by each source. The hook picks the first source in spec 4.2's order that is set.
    """

    repos = {name: _repo(tmp_path / name, name) for name in ("one", "two", "three", "four")}
    data_dir = _vault(tmp_path, *repos.values())
    _on(monkeypatch)
    args: list[str] = []
    payload: dict[str, str] = {}
    if "argument" in sources:
        args = ["--project-dir", str(repos["one"])]
    if "env" in sources:
        monkeypatch.setenv("ALICE_PROJECT_DIR", str(repos["two"]))
    if "stdin" in sources:
        payload = {"cwd": str(repos["three"])}
    monkeypatch.chdir(repos["four"])
    out = _hook(monkeypatch, capsys, data_dir, payload, *args)
    assert f'Alice project: "{expected}"' in out
    assert f'Fact of project {expected} only' in out
    for other in set(repos) - {expected}:
        assert f"Fact of project {other} only" not in out


@pytest.mark.parametrize(
    "cwd",
    [
        "relative/path",
        "/nonexistent/folder/for/the/hook",
        42,
        None,
        ["/tmp"],
        "",
    ],
    ids=["relative", "missing", "number", "null", "list", "empty"],
)
def test_a_stdin_cwd_that_is_not_an_existing_absolute_folder_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cwd: object
) -> None:
    """Mutation: accept a relative or missing ``cwd``, or fail the hook on a value that is not a string.

    The next source, the working folder, decides. Here it is a repository, so its project shows.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": cwd})
    assert 'Alice project: "payments"' in out


def test_a_payload_over_64_kib_is_not_parsed_and_is_read_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: parse an oversize payload, or leave its tail unread so the host's write could block.

    The ``cwd`` sits first, in front of a long padding value, and the payload is over the limit. The hook ignores
    it and uses the working folder, and the whole input has been consumed.
    """

    named = _repo(tmp_path / "named", "named")
    working = _repo(tmp_path / "working", "working")
    data_dir = _vault(tmp_path, named, working)
    monkeypatch.chdir(working)
    _on(monkeypatch)
    body = json.dumps({"cwd": str(named), "padding": "x" * (MAX_HOOK_PAYLOAD_BYTES + 10)}).encode()
    stream = io.BytesIO(body)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(stream, encoding="utf-8"))
    assert session_start_hook.main(["--format", "markdown", "--data-dir", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert 'Alice project: "working"' in out
    assert stream.tell() == len(body), "the whole payload was read"


def test_a_payload_that_is_not_json_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: let a payload that does not parse fail the hook.

    A host that sends something else on stdin still gets a brief, from the working folder.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, raw=b"\xff\xfe not json at all")
    assert 'Alice project: "payments"' in out


def test_a_folder_outside_any_repository_gets_the_none_line_and_the_whole_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: print no line when no project is found, or report it as a failure.

    The brief says so in one plain line, and every note shows as before.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(outside)})
    lines = out.splitlines()
    assert lines[1] == STATUS_LINE_NONE
    assert "Fact of project payments only" in out
    assert "(global)" not in out


@pytest.mark.parametrize(
    "break_repo",
    [
        pytest.param(lambda git: (git / "config").write_text("x" * (256 * 1024 + 1)), id="config-over-256-kib"),
        pytest.param(lambda git: (git / "config").unlink(), id="config-missing"),
        pytest.param(lambda git: (git / "config").write_text("[remote \"origin\"\n\turl = broken"), id="config-malformed"),
        pytest.param(lambda git: (git / "config").write_text("[include]\n\tpath = elsewhere\n"), id="config-with-include"),
    ],
)
def test_a_git_directory_that_cannot_be_read_fails_the_detection_and_not_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], break_repo
) -> None:
    """Mutation: let the exception escape, or print the unscoped brief with no line for a failed detection.

    A git directory that was found and could not be read within the caps is a failed detection. The hook exits 0
    and the brief says so in its own line, which differs from the line for a folder that is no repository.
    """

    repo = make_repo(tmp_path / "broken", config=config_text("https://example.com/acme/broken.git"))
    break_repo(repo / ".git")
    good = _repo(tmp_path / "good", "good")
    data_dir = _vault(tmp_path, good)
    monkeypatch.chdir(repo)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    assert out.splitlines()[1] == STATUS_LINE_FAILED
    assert STATUS_LINE_NONE not in out
    assert "Fact of project good only" in out, "a failed detection reads the whole vault"


def test_a_gitdir_file_that_is_too_big_or_malformed_fails_the_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: treat an oversize or malformed ``.git`` file as no repository."""

    data_dir = _vault(tmp_path, _repo(tmp_path / "good", "good"))
    _on(monkeypatch)
    for name, text in (("big", "gitdir: " + "a" * 5000), ("malformed", "this is not a gitdir line\n")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / ".git").write_text(text)
        out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(folder)})
        assert out.splitlines()[1] == STATUS_LINE_FAILED, name


def test_a_resolver_that_raises_is_a_failed_detection_and_the_hook_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: let an exception from the resolver reach the hook's caller, or report ``none``.

    The brief carries the failure line, in both output formats.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    _on(monkeypatch)

    def boom(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(project_view_module, "detect_project", boom)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    assert out.splitlines()[1] == STATUS_LINE_FAILED
    assert "resolver blew up" not in out
    json_out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)}, fmt="json")
    assert STATUS_LINE_FAILED in json.loads(json_out)["additional_context"]


def test_an_unwritable_vault_keeps_the_hooks_existing_fail_open_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: print a project line, or fail, when the vault cannot be opened.

    A data directory that is a regular file cannot hold a vault. The hook prints its existing fail-open output,
    ``{}`` in the JSON format and a blank line in the markdown format, and exits 0, with scoping on.
    """

    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file")
    repo = _repo(tmp_path / "payments", "payments")
    monkeypatch.chdir(repo)
    _on(monkeypatch)
    assert _hook(monkeypatch, capsys, blocker, {"cwd": str(repo)}, fmt="json").strip() == "{}"
    assert _hook(monkeypatch, capsys, blocker, {"cwd": str(repo)}, fmt="markdown").strip() == ""


def test_the_vault_setting_turns_the_hook_on_without_an_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: read only the environment, or ignore the vault row.

    Most hosts cannot be given an environment variable per entry, so ``alice-memory project scoping on`` saves the
    switch in the vault and the hook reads it. The environment still beats it.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path, repo)
    monkeypatch.chdir(repo)
    assert 'Alice project:' not in _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    assert onramp_main(["project", "scoping", "on", "--data-dir", str(data_dir)]) == 0
    capsys.readouterr()
    assert 'Alice project: "payments"' in _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    assert out.splitlines()[1] == STATUS_LINE_OFF


def test_the_home_folder_is_never_a_project_for_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: drop the home-folder stop in the walk up.

    A dotfiles repository at the home folder does not make every session in it a project (spec 4.3).
    """

    home = tmp_path / "home"
    make_repo(home, config=config_text("https://example.com/me/dotfiles.git"))
    nested = home / "notes" / "today"
    nested.mkdir(parents=True)
    data_dir = _vault(tmp_path)
    monkeypatch.chdir(nested)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(nested)})
    assert out.splitlines()[1] == STATUS_LINE_NONE


def test_the_hook_output_stays_inside_the_character_cap_with_the_project_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: add the line after the cap is applied.

    Long notes fill the brief. With the project line the hook output is still at most 9,500 characters.
    """

    repo = _repo(tmp_path / "payments", "payments")
    data_dir = _vault(tmp_path)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(8):
            add_memory(store, key=f"fact.long.{index}", text=f"Long number {index} " + "filler words " * 120, scope=(_ids(repo)[0],))
    monkeypatch.chdir(repo)
    _on(monkeypatch)
    out = _hook(monkeypatch, capsys, data_dir, {"cwd": str(repo)})
    assert brief_char_len(out.rstrip("\n")) <= 9_500
    assert out.splitlines()[1].startswith("Alice project:")
