"""The command line wiring of the harness (scripts/alice_bench.py): the lines that decide what a number measured.

A library function that is right proves little when the command that calls it passes the wrong
argument. These tests drive the commands themselves, in a process of their own where the process
is what matters (which checkout is imported, what the environment holds) and in this process where
it is not, and they check the lines that say which arm gets which cap, which environment reaches a
run, which checkout is imported, what the manifest and the fingerprint record, and what a build may
write where. Everything runs on invented files and needs no network, no model and no paid call.

Every test names the mutation that must fail it. Test ids (TH2, TH3, TH8, TH9, ...) follow the
spec of the search-quality release.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.alice_bench as bench

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "apps" / "api" / "src"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "search_quality"
CORPUS = FIXTURES / "corpus"
QUESTIONS = FIXTURES / "questions.json"
BENCH_SCRIPT = REPO_ROOT / "scripts" / "alice_bench.py"

QUERY = "Who has the spare key to the brass cabinet?"


# Helpers ---------------------------------------------------------------------


def _env(pythonpath: Path | None = SRC) -> dict[str, str]:
    """The parent environment without git variables (a git hook sets some) and with the given import path."""

    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_") and name != "PYTHONPATH"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return env


def _cli(*args: str, pythonpath: Path | None = SRC) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_env(pythonpath),
        timeout=300,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """A git command in a temporary repository, with a throwaway identity and no hooks or signing."""

    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=bench",
            "-c",
            "user.email=",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_env(None),
        timeout=120,
    )


def _small_repo(path: Path) -> Path:
    """A git repository with one tracked file under apps and one under docs, committed."""

    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / ".gitignore").write_text("*.log\n__pycache__/\n", encoding="utf-8")
    (path / "apps").mkdir()
    (path / "apps" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "docs" / "page.md").write_text("page\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "first")
    return path


def _second_checkout(root: Path) -> Path:
    """A second checkout: a git repository that holds a copy of the package source."""

    target = root / "second-checkout"
    shutil.copytree(SRC, target / "apps" / "api" / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    _git(target, "init", "-q")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "copy of the package source")
    return target


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _state(run: Path, name: str, label: str) -> Path:
    """A counter folder of one answering run: inside the run directory, as the harness requires."""

    return run / bench.STATE_DIRNAME / f"{name}-{label}"


def _manifest(run: Path) -> dict[str, Any]:
    return dict(json.loads((run / bench.MANIFEST_FILENAME).read_text(encoding="utf-8")))


def _log(state: Path) -> list[dict[str, Any]]:
    path = state / "search_log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One built vault of the invented fixture corpus, shared by the tests that only read it."""

    path = tmp_path_factory.mktemp("wiring") / "run"
    assert bench.main(["build", "--run-dir", str(path), "--corpus", str(CORPUS)]) == 0
    return path


@pytest.fixture(scope="module")
def big_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A vault whose snapshot is large enough that one grep prints more than the 16 KB cap."""

    root = tmp_path_factory.mktemp("big")
    corpus = root / "corpus"
    corpus.mkdir()
    lines = [f"## Lamp {number}\n\nThe lamp number {number} burns paraffin oil at the north window.\n" for number in range(300)]
    (corpus / "lamps.md").write_text("# Lamps\n\n" + "\n".join(lines), encoding="utf-8")
    path = root / "run"
    assert bench.main(["build", "--run-dir", str(path), "--corpus", str(corpus)]) == 0
    return path


# TH9: which checkout is imported, and what says so -----------------------------


def test_git_state_reads_a_commit_and_calls_a_checkout_dirty_when_product_code_differs(tmp_path: Path) -> None:
    """TH9. The dirty flag is true for a tracked edit, a staged edit and an untracked file under apps or workers.

    Mutation: ``dirty = False``, drop the ``git diff --quiet HEAD`` test, or drop the untracked-file
    test. A checkout whose product code differs from its commit would then read as clean.
    """

    repo = _small_repo(tmp_path / "repo")
    head = _head(repo)
    clean = bench.git_state(repo)
    assert clean == {"git_sha": head, "dirty": False}

    tool = repo / "apps" / "tool.py"
    tool.write_text("VALUE = 2\n", encoding="utf-8")
    assert bench.git_state(repo) == {"git_sha": head, "dirty": True}, "a tracked edit"
    _git(repo, "add", "apps/tool.py")
    assert bench.git_state(repo)["dirty"] is True, "a staged edit"
    _git(repo, "commit", "-q", "-m", "second")
    assert bench.git_state(repo) == {"git_sha": _head(repo), "dirty": False}
    assert _head(repo) != head

    extra = repo / "apps" / "new_module.py"
    extra.write_text("X = 1\n", encoding="utf-8")
    assert bench.git_state(repo)["dirty"] is True, "an untracked file under apps"
    extra.unlink()
    (repo / "workers").mkdir()
    (repo / "workers" / "job.py").write_text("Y = 1\n", encoding="utf-8")
    assert bench.git_state(repo)["dirty"] is True, "an untracked file under workers"
    (repo / "workers" / "job.py").unlink()
    (repo / "apps" / "debug.log").write_text("ignored\n", encoding="utf-8")
    (repo / "docs" / "scratch.md").write_text("not product code\n", encoding="utf-8")
    assert bench.git_state(repo)["dirty"] is False, "an ignored file and a file outside apps and workers do not count"

    assert bench.git_state(tmp_path) == {"git_sha": None, "dirty": None}, "not a repository"


def test_the_inside_checkout_flag_is_false_when_the_package_came_from_another_tree(tmp_path: Path) -> None:
    """TH9. The fingerprint says whether ``alicebot_api`` was imported from the checkout it names.

    Mutation: set ``alicebot_api_inside_checkout`` to ``True``, or compare against the wrong folder.
    Every check on the repository's own tree passes that, and only a second tree shows it.
    """

    common: dict[str, Any] = {
        "gates": bench.load_gates(),
        "session": None,
        "manifest": None,
        "search_quality": None,
        "question_set_sha256": None,
    }
    own = bench.fingerprint(repo=REPO_ROOT, **common)
    assert own["alicebot_api_inside_checkout"] is True
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = bench.fingerprint(repo=elsewhere, **common)
    assert other["alicebot_api_inside_checkout"] is False
    assert other["git_sha"] is None and other["dirty"] is None
    assert other["alicebot_api_file"] == own["alicebot_api_file"]


def test_checkout_imports_the_named_tree_and_the_fingerprint_and_manifest_say_so(tmp_path: Path) -> None:
    """TH9. ``--checkout`` puts that tree first on the path, in a process whose import path names another.

    The import path of these processes names this repository, as a worktree venv that imports the main
    checkout does. A build at the second checkout then has to import the second checkout, record its
    commit, its dirty flag and the real path, and refuse a later run that is another checkout, another
    commit or another dirty state.

    Mutation: drop ``sys.path.insert`` from ``activate_checkout`` (the repository imports itself and the
    run is refused as an import from outside), pass the wrong repo to ``git_state``, or stop writing the
    build record into the manifest or the fingerprint.
    """

    other = _second_checkout(tmp_path)
    source = (other / "apps" / "api" / "src").resolve()
    run_dir = tmp_path / "run"
    built = _cli("build", "--run-dir", str(run_dir), "--corpus", str(CORPUS), "--checkout", str(other))
    assert built.returncode == 0, built.stderr
    head = _head(other)
    build = _manifest(run_dir)["build"]
    assert build["git_sha"] == head and build["dirty"] is False and build["search_quality"] == "unset"
    assert Path(build["alicebot_api_file"]).is_relative_to(source)

    shown = _cli("fingerprint", "--run-dir", str(run_dir), "--checkout", str(other))
    assert shown.returncode == 0, shown.stderr
    fingerprint = json.loads(shown.stdout)
    assert fingerprint["git_sha"] == head and fingerprint["dirty"] is False
    assert Path(fingerprint["alicebot_api_file"]).is_relative_to(source)
    assert fingerprint["alicebot_api_inside_checkout"] is True
    assert fingerprint["vault_build"] == build

    # The same vault, read by the repository this test runs in: another checkout.
    refused = _cli("recall", "--run-dir", str(run_dir), "--query", "spare key")
    assert refused.returncode == bench.EXIT_REFUSED and refused.stdout == ""
    assert "built by a different checkout" in refused.stderr

    # A tracked edit makes the checkout dirty, and the vault was built clean.
    init_file = other / "apps" / "api" / "src" / "alicebot_api" / "__init__.py"
    init_file.write_text(init_file.read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8")
    dirty = _cli("fingerprint", "--run-dir", str(run_dir), "--checkout", str(other))
    assert dirty.returncode == bench.EXIT_REFUSED and "(dirty differ)" in dirty.stderr
    rebuilt = _cli("build", "--run-dir", str(run_dir), "--corpus", str(CORPUS), "--checkout", str(other), "--rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert _manifest(run_dir)["build"]["dirty"] is True
    now_dirty = json.loads(_cli("fingerprint", "--run-dir", str(run_dir), "--checkout", str(other)).stdout)
    assert now_dirty["dirty"] is True and now_dirty["vault_build"]["dirty"] is True

    # A commit makes it clean again at another sha: the dirty flag differs, and so does the commit.
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "an edit")
    moved = _cli("fingerprint", "--run-dir", str(run_dir), "--checkout", str(other))
    assert moved.returncode == bench.EXIT_REFUSED
    assert "(git_sha, dirty differ)" in moved.stderr
    assert _head(other) != head


def test_a_checkout_is_refused_when_the_package_was_already_imported_from_elsewhere(run: Path, tmp_path: Path) -> None:
    """TH9. Naming a checkout after another tree was imported cannot quietly measure the wrong code.

    The first call names the tree that is already imported and works. The second names another and is
    refused for the import, before any check of the vault.

    Mutation: remove the ``is_relative_to`` refusal from ``activate_checkout``. The run would carry on
    with the wrong tree and fail later for another reason, so the message is asserted.
    """

    other = tmp_path / "other"
    (other / "apps" / "api" / "src" / "alicebot_api").mkdir(parents=True)
    (other / "apps" / "api" / "src" / "alicebot_api" / "__init__.py").write_text("", encoding="utf-8")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import alicebot_api\n"
        "import scripts.alice_bench as bench\n"
        f"first = bench.main(['fingerprint', '--run-dir', {str(run)!r}, '--checkout', {str(REPO_ROOT)!r}])\n"
        f"second = bench.main(['fingerprint', '--run-dir', {str(run)!r}, '--checkout', {str(other)!r}])\n"
        "print('RESULT', first, second)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, env=_env(SRC), timeout=300
    )
    assert completed.stdout.strip().splitlines()[-1] == f"RESULT {bench.EXIT_OK} {bench.EXIT_REFUSED}", completed.stderr
    assert "already imported from outside the checkout" in completed.stderr


def test_a_vault_is_read_only_by_the_checkout_that_built_it() -> None:
    """TH9. Commit, dirty flag and import path must each match the build record.

    Mutation: leave one key out of ``CHECKOUT_IDENTITY_KEYS``, accept a manifest with no build record,
    or accept no manifest at all.
    """

    identity = bench.checkout_identity(REPO_ROOT)
    assert set(identity) == {"git_sha", "dirty", "alicebot_api_file"}
    assert tuple(identity) == bench.CHECKOUT_IDENTITY_KEYS
    bench.require_matching_build({"build": {**identity, "search_quality": "unset"}}, REPO_ROOT)
    other_values = {"git_sha": "0" * 40, "dirty": not identity["dirty"], "alicebot_api_file": "/another/tree/alicebot_api/__init__.py"}
    for key in bench.CHECKOUT_IDENTITY_KEYS:
        altered = {**identity, key: other_values[key]}
        with pytest.raises(bench.CheckoutError, match=rf"\({key} differ\)"):
            bench.require_matching_build({"build": altered}, REPO_ROOT)
    with pytest.raises(bench.CheckoutError, match=r"\(git_sha, dirty, alicebot_api_file differ\)"):
        bench.require_matching_build({"build": other_values}, REPO_ROOT)
    with pytest.raises(bench.RunDirError, match="holds no build"):
        bench.require_matching_build(None, REPO_ROOT)
    for unrecorded in ({}, {"build": {}}, {"build": {"git_sha": identity["git_sha"]}}, {"build": "text"}):
        with pytest.raises(bench.CheckoutError, match="does not say which checkout"):
            bench.require_matching_build(unrecorded, REPO_ROOT)


def test_every_command_that_reads_a_vault_refuses_one_that_another_checkout_built(
    run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH9. recall, fingerprint, batch, search (both arms) and anchors all pass through the build check.

    The manifest of a copy of the vault claims another commit. A command that skipped the check would
    read the copy and write output.

    Mutation: drop ``require_matching_build`` from ``_context``, or call it only for some commands.
    """

    copy = tmp_path / "copy"
    shutil.copytree(run, copy)
    manifest = _manifest(copy)
    manifest["build"]["git_sha"] = "0" * 40
    (copy / bench.MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "outputs.json"
    state = copy / bench.STATE_DIRNAME / "refused"
    commands = [
        ["recall", "--query", "spare key"],
        ["fingerprint"],
        ["batch", "--questions", str(QUESTIONS), "--out", str(out)],
        ["search", "--arm", "alice", "--query", "spare key", "--state-dir", str(state)],
        ["search", "--arm", "grep", "--pattern", "key", "--state-dir", str(state)],
        ["search", "--arm", "grep-uncapped", "--pattern", "key", "--state-dir", str(state)],
        ["anchors", "--questions", str(QUESTIONS)],
    ]
    for command in commands:
        assert bench.main([command[0], "--run-dir", str(copy), *command[1:]]) == bench.EXIT_REFUSED, command
        captured = capsys.readouterr()
        assert captured.out == "", command
        assert "built by a different checkout (git_sha differ)" in captured.err, command
    assert not out.exists() and not state.exists()

    unbuilt = tmp_path / "unbuilt"
    unbuilt.mkdir()
    (unbuilt / bench.RUN_MARKER).write_text("marker\n", encoding="utf-8")
    assert bench.main(["recall", "--run-dir", str(unbuilt), "--query", "key"]) == bench.EXIT_REFUSED
    assert "holds no build" in capsys.readouterr().err


def test_the_manifest_records_the_checkout_and_the_switch_that_built_the_vault(tmp_path: Path) -> None:
    """TH9. The build record names the checkout and the switch, and the labels the importer was given.

    Mutation: drop ``search_quality`` from the call in ``_cmd_build`` or from ``build_vault``, stop
    passing ``--sensitivity`` or ``--domain`` to the importer, or stop writing them to the manifest.
    """

    default = tmp_path / "default"
    assert bench.main(["build", "--run-dir", str(default), "--corpus", str(CORPUS)]) == 0
    manifest = _manifest(default)
    identity = bench.checkout_identity(REPO_ROOT)
    assert manifest["build"] == {**identity, "search_quality": "unset"}
    assert (manifest["domain"], manifest["sensitivity"]) == ("project", "internal")
    switched = tmp_path / "switched"
    args = ["build", "--run-dir", str(switched), "--corpus", str(CORPUS), "--search-quality", "passage"]
    assert bench.main([*args, "--sensitivity", "public", "--domain", "learning"]) == 0
    assert _manifest(switched)["build"]["search_quality"] == "passage"
    assert (_manifest(switched)["domain"], _manifest(switched)["sensitivity"]) == ("learning", "public")
    for run_dir, domain, sensitivity in ((default, "project", "internal"), (switched, "learning", "public")):
        connection = sqlite3.connect(f"file:{run_dir / bench.VAULT_DIRNAME / bench.VAULT_FILENAME}?mode=ro", uri=True)
        try:
            labels = connection.execute("SELECT DISTINCT domain, sensitivity FROM sources").fetchall()
        finally:
            connection.close()
        assert labels == [(domain, sensitivity)]


# TH7 and TH9: the switch, and what a batch saves ---------------------------------


def test_the_switch_reaches_the_run_and_a_parent_value_never_does(
    run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH7 and TH9. ``--search-quality`` sets the variable for the run and the fingerprint records it.

    The parent holds ``ALICE_SEARCH_QUALITY=on`` and an agent key. A run with the flag sees only the
    flag's value, a run without it sees neither, and the parent's values come back after.

    Mutation: make ``arm_environment`` return ``{}`` (the switch never reaches the run), stop scrubbing
    the parent's value, or drop ``search_quality`` from the batch fingerprint.
    """

    seen: list[tuple[str | None, str | None]] = []
    original = bench.McpSession.recall

    def recording(self: bench.McpSession, query: str) -> str:
        seen.append((os.environ.get("ALICE_SEARCH_QUALITY"), os.environ.get("ALICE_AGENT_API_KEY")))
        return original(self, query)

    monkeypatch.setattr(bench.McpSession, "recall", recording)
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "on")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", "not-a-real-key")
    base = ["batch", "--run-dir", str(run), "--questions", str(QUESTIONS)]
    with_flag = tmp_path / "with.json"
    assert bench.main([*base, "--out", str(with_flag), "--search-quality", "passage"]) == 0
    assert seen and set(seen) == {("passage", None)}
    assert json.loads(with_flag.read_text())["fingerprint"]["search_quality"] == "passage"
    seen.clear()
    without_flag = tmp_path / "without.json"
    assert bench.main([*base, "--out", str(without_flag)]) == 0
    assert seen and set(seen) == {(None, None)}
    assert json.loads(without_flag.read_text())["fingerprint"]["search_quality"] == "unset"
    assert os.environ["ALICE_SEARCH_QUALITY"] == "on" and os.environ["ALICE_AGENT_API_KEY"] == "not-a-real-key"
    capsys.readouterr()
    for value in ("off", "on"):
        assert bench.main(["fingerprint", "--run-dir", str(run), "--search-quality", value]) == 0
        assert json.loads(capsys.readouterr().out)["search_quality"] == value


def test_a_batch_saves_each_variants_recall_and_a_fingerprint_that_can_be_read_back(run: Path, tmp_path: Path) -> None:
    """TH9. The outputs are the recall of each variant's own query, with the fingerprint of the run beside them.

    Mutation: run the verbatim question for both variants, drop a model id or the question set hash from
    the fingerprint, or save a fingerprint of another vault.
    """

    out = tmp_path / "outputs.json"
    code = bench.main(
        [
            "batch",
            "--run-dir",
            str(run),
            "--questions",
            str(QUESTIONS),
            "--out",
            str(out),
            "--answerer-model",
            "answerer-x",
            "--judge-model",
            "judge-y",
        ]
    )
    assert code == 0
    document = json.loads(out.read_text())
    qset = bench.load_questions(QUESTIONS)
    manifest = _manifest(run)
    fingerprint = document["fingerprint"]
    assert document["question_set_sha256"] == fingerprint["question_set_sha256"] == qset.sha256
    assert fingerprint["git_sha"] == bench.git_state(REPO_ROOT)["git_sha"]
    assert fingerprint["import_order"] == manifest["order"] == "sorted"
    assert fingerprint["corpus_hash"] == manifest["corpus_hash"]
    assert fingerprint["snapshot_hash"] == manifest["snapshot_hash"]
    assert fingerprint["vault_build"] == manifest["build"]
    assert fingerprint["gates_sha256"] == bench.load_gates().sha256
    assert (fingerprint["recall_limit"], fingerprint["grep_cap_bytes"], fingerprint["byte_budgets"]) == (8, 16384, [4096, 8192])
    assert (fingerprint["answerer_model"], fingerprint["judge_model"]) == ("answerer-x", "judge-y")
    with bench.scoped_environment():
        session = bench.McpSession(run / bench.VAULT_DIRNAME / bench.VAULT_FILENAME)
        assert fingerprint["tools_list_digest"] == bench.tools_list_digest(session.tools_list())
        for question in qset.questions:
            assert document["outputs"][question.id]["verbatim"] == session.recall(question.question)
            assert document["outputs"][question.id]["keyword"] == session.recall(question.keyword_query or "")
    assert any(
        document["outputs"][question.id]["keyword"] != document["outputs"][question.id]["verbatim"]
        for question in qset.questions
    )


def test_the_corpus_hash_follows_every_file_name_and_every_byte(tmp_path: Path) -> None:
    """TH9. The hash that names a corpus changes when a byte, a name or the set of files changes.

    Mutation: hash only the file names (ignore ``raw_sha256``), or hash the files in the order they
    were read. A changed corpus would then keep its hash and a vault would be reused for it.
    """

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# Alpha\n\nalpha text here\n", encoding="utf-8")
    (corpus / "b.md").write_text("# Beta\n\nbeta text here\n", encoding="utf-8")

    def current() -> str:
        return bench.corpus_hash(bench.read_corpus(corpus)[1])

    base = current()
    assert current() == base and len(base) == 64
    files = bench.read_corpus(corpus)[1]
    assert bench.corpus_hash(list(reversed(files))) == base, "the order the files were read in does not matter"
    (corpus / "b.md").write_text("# Beta\n\nbeta text herE\n", encoding="utf-8")
    one_byte = current()
    assert one_byte != base
    (corpus / "b.md").write_text("# Beta\n\nbeta text here\n", encoding="utf-8")
    assert current() == base
    (corpus / "b.md").rename(corpus / "c.md")
    assert current() != base
    (corpus / "c.md").rename(corpus / "b.md")
    (corpus / "d.md").write_text("# Delta\n\ndelta text here\n", encoding="utf-8")
    assert current() != base


# TH2 and TH3: the search command ---------------------------------------------------


def test_the_capped_grep_arm_is_cut_the_uncapped_arm_is_not_and_the_log_records_both(
    big_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH2 and TH3. One byte cap binds the ``grep`` arm and nothing binds ``grep-uncapped``.

    The log holds the search input, the raw size, whether it was cut and the sha1 of the text shown.

    Mutation: give the capped arm no cap, swap the two arms at the line that picks the cap in
    ``_cmd_search``, or blank the log's ``input``, ``bytes`` or ``sha1`` or its ``arm`` name.
    """

    cap = bench.load_gates().grep_cap
    assert cap == 16384
    shown: dict[str, tuple[str, dict[str, Any]]] = {}
    for arm in ("grep", "grep-uncapped"):
        state = _state(big_run, tmp_path.name, arm)
        args = ["search", "--arm", arm, "--run-dir", str(big_run), "--state-dir", str(state)]
        assert bench.main([*args, "--pattern", "paraffin", "--grep-options=-n"]) == 0
        output = capsys.readouterr().out
        lines = _log(state)
        assert len(lines) == 1
        shown[arm] = (output, lines[0])
    capped, capped_log = shown["grep"]
    uncapped, uncapped_log = shown["grep-uncapped"]
    notice = f"[output cut at {cap} bytes]\n"
    assert capped.endswith(notice) and notice not in uncapped
    assert len(capped.encode()) == cap + 1 + len(notice)
    assert len(uncapped.encode()) > cap
    assert uncapped.startswith(capped.split("\n[output cut", 1)[0])
    assert (capped_log["arm"], uncapped_log["arm"]) == ("grep", "grep-uncapped")
    assert (capped_log["truncated"], uncapped_log["truncated"]) == (True, False)
    assert capped_log["bytes"] == uncapped_log["bytes"] == len(uncapped.encode())
    assert capped_log["sha1"] == hashlib.sha1(capped.encode()).hexdigest()
    assert uncapped_log["sha1"] == hashlib.sha1(uncapped.encode()).hexdigest()
    assert capped_log["sha1"] != uncapped_log["sha1"]
    for entry in (capped_log, uncapped_log):
        assert json.loads(entry["input"]) == {"options": "-n", "pattern": "paraffin"}
        assert entry["status"] == "ok" and entry["n"] == 1


def test_the_alice_arm_logs_its_query_and_the_size_and_hash_of_what_it_returned(
    run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH2. The log of the Alice arm names the query and describes the text the agent was shown.

    Mutation: blank ``input``, ``bytes`` or ``sha1`` of the log line, or log the query of another arm.
    """

    state = _state(run, tmp_path.name, "alice")
    assert bench.main(["search", "--arm", "alice", "--run-dir", str(run), "--state-dir", str(state), "--query", QUERY]) == 0
    output = capsys.readouterr().out
    assert output.endswith("\n") and not output.endswith("\n\n")
    text = output[:-1]
    entry = _log(state)[0]
    assert entry["input"] == QUERY and entry["arm"] == "alice" and entry["status"] == "ok"
    assert entry["bytes"] == len(text.encode()) and entry["truncated"] is False
    assert entry["sha1"] == hashlib.sha1(text.encode()).hexdigest()
    with bench.scoped_environment():
        assert text == bench.McpSession(run / bench.VAULT_DIRNAME / bench.VAULT_FILENAME).recall(QUERY)
    assert json.loads(text)["sources"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--limit", "50"],
        ["--limit", "1"],
        ["--context-depth", "high"],
        ["--include-sources", "false"],
        ["--include-sources", "maybe"],
    ],
)
def test_the_search_command_refuses_each_pinned_setting_and_spends_nothing(
    run: Path, tmp_path: Path, flags: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """TH3. ``limit``, ``context_depth`` and ``include_sources`` stay at their defaults at the command line.

    Mutation: make ``_parse_bool_option`` return ``None`` for every value (``false`` would pass), or
    accept a ``--limit`` or ``--context-depth`` other than the default.
    """

    state = _state(run, tmp_path.name, "pinned")
    common = ["search", "--arm", "alice", "--run-dir", str(run), "--state-dir", str(state), "--query", "spare key"]
    assert bench.main([*common, *flags]) == bench.EXIT_REFUSED
    refusal = capsys.readouterr()
    assert refusal.out == "" and refusal.err.startswith("refused: ")
    assert not (state / "search.count").exists()
    allowed = ["--limit", "8", "--context-depth", "low", "--include-sources", "true"]
    assert bench.main([*common, *allowed]) == 0
    assert (state / "search.count").read_text() == "1"
    capsys.readouterr()


def test_the_budget_is_three_by_default_and_a_flag_can_lower_it(
    run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH2. ``searches_per_run`` of ``gates.json`` is the default and ``--budget`` can only change the count asked for.

    Mutation: ignore ``--budget``, or ignore ``searches_per_run`` and pick another default.
    """

    assert bench.load_gates().searches_per_run == 3

    def search(state: Path, *extra: str) -> int:
        args = ["search", "--arm", "grep", "--run-dir", str(run), "--state-dir", str(state), "--pattern", "key", *extra]
        code = bench.main(args)
        capsys.readouterr()
        return code

    default = _state(run, tmp_path.name, "default")
    assert [search(default) for _ in range(4)] == [0, 0, 0, bench.EXIT_BUDGET]
    lowered = _state(run, tmp_path.name, "lowered")
    assert [search(lowered, "--budget", "1") for _ in range(2)] == [0, bench.EXIT_BUDGET]
    assert len(_log(lowered)) == 1 and len(_log(default)) == 3


def test_a_search_that_raises_anything_is_counted_logged_and_answered_in_plain_words(tmp_path: Path) -> None:
    """TH2. The count and the log agree even when a runner fails in a way nobody planned for.

    Mutation: catch only ``BenchError`` in ``run_budgeted_search``. An ``OSError`` would then escape as a
    traceback with the search counted and no log line written.
    """

    def exploding() -> bench.SearchResult:
        raise OSError("the disk is gone at /secret/place")

    result = bench.run_budgeted_search(tmp_path, arm="grep", search_input="x", budget=3, runner=exploding)
    assert result.status == "error" and result.truncated is False
    assert result.text == "error: the search failed (OSError)"
    assert "secret" not in result.text
    assert (tmp_path / "search.count").read_text() == "1"
    lines = _log(tmp_path)
    assert [(line["n"], line["status"], line["input"]) for line in lines] == [(1, "error", "x")]
    ok = bench.SearchResult(text="fine", status="ok", truncated=False, raw_bytes=4)
    for _ in range(2):
        bench.run_budgeted_search(tmp_path, arm="grep", search_input="y", budget=3, runner=lambda: ok)
    assert len(_log(tmp_path)) == 3 and (tmp_path / "search.count").read_text() == "3"
    with pytest.raises(bench.BudgetExhausted):
        bench.run_budgeted_search(tmp_path, arm="grep", search_input="z", budget=3, runner=lambda: ok)


def test_a_pattern_that_looks_like_an_option_is_searched_for_and_never_obeyed(tmp_path: Path) -> None:
    """TH3. The pattern goes to grep behind ``-e``, so a leading dash cannot turn it into an option.

    ``-f`` followed by a path would make grep read its patterns from that file. The file below holds the
    word ``needle``, which a line of the snapshot contains.

    The flags an agent may add are checked inside ``grep_search`` too, so a caller that skips the
    command line check still cannot pass ``-f`` or ``--include``.

    Mutation: drop ``-e`` from the grep call in ``grep_search``, or replace its ``validate_grep_options``
    call with a plain split. ``-rn`` would be read as flags, ``-f`` and a path would match the needle
    line, and an option that reads a file would reach grep.
    """

    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "notes.md").write_text(
        "use the -rn flag\nthe --include=nothing switch\nthe needle sits in a haystack\n", encoding="utf-8"
    )
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("needle\n", encoding="utf-8")
    for pattern, expected in (("-rn", "use the -rn flag"), ("--include=nothing", "the --include=nothing switch")):
        found = bench.grep_search(snapshot, pattern=pattern, options="", cap=None)
        assert found.status == "ok"
        assert found.text == f"notes.md:{expected}\n"
    injected = bench.grep_search(snapshot, pattern=f"-f{patterns}", options="", cap=None)
    assert injected.text == "" and injected.status == "ok"
    for options in (f"-f {patterns}", "--include=*.md", "-e needle", "-i;ls"):
        with pytest.raises(bench.GrepOptionError):
            bench.grep_search(snapshot, pattern="needle", options=options, cap=None)
    control = bench.grep_search(snapshot, pattern="needle", options="", cap=None)
    assert "needle sits" in control.text


def test_grep_does_not_inherit_grep_options_or_colour_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TH3. The environment grep runs in carries none of the variables that change what it prints.

    A recording wrapper reads the environment ``grep_search`` passes, so the test does not depend on
    whether this machine's grep still reads ``GREP_OPTIONS``.

    Mutation: stop removing ``GREP_OPTIONS``, ``GREP_COLOR`` or ``GREP_COLORS`` from the environment
    in ``grep_search``, or pass an empty environment.
    """

    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "a.md").write_text("one line\n", encoding="utf-8")
    monkeypatch.setenv("GREP_OPTIONS", "-v")
    monkeypatch.setenv("GREP_COLOR", "1;31")
    monkeypatch.setenv("GREP_COLORS", "mt=01;31")
    seen: dict[str, dict[str, str]] = {}
    real_run = subprocess.run

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen["env"] = dict(kwargs["env"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    result = bench.grep_search(snapshot, pattern="line", options="", cap=None)
    assert result.text == "a.md:one line\n"
    assert not {"GREP_OPTIONS", "GREP_COLOR", "GREP_COLORS"} & set(seen["env"])
    assert seen["env"]["PATH"] == os.environ["PATH"]


def test_grep_that_cannot_run_or_fails_is_a_plain_refusal_and_never_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TH3. An empty snapshot, a missing grep, a bad pattern and a slow grep each stop with a message.

    Mutation: remove the empty-snapshot check, the missing-binary check, the exit status check or the
    timeout conversion in ``grep_search``. Each would end in a traceback, or in empty output that an
    agent reads as "no match".
    """

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(bench.BenchError, match="snapshot holds no files"):
        bench.grep_search(empty, pattern="x", options="", cap=None)
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "a.md").write_text("one line\n", encoding="utf-8")
    with pytest.raises(bench.BenchError, match="grep failed"):
        bench.grep_search(snapshot, pattern="(", options="-E", cap=None)
    no_match = bench.grep_search(snapshot, pattern="absent", options="", cap=None)
    assert (no_match.text, no_match.status, no_match.truncated, no_match.raw_bytes) == ("", "ok", False, 0)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(bench.BenchError, match="grep is not installed"):
        bench.grep_search(snapshot, pattern="line", options="", cap=None)
    monkeypatch.undo()

    def slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="grep", timeout=1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(bench.BenchError, match="grep timed out"):
        bench.grep_search(snapshot, pattern="line", options="", cap=None)


def test_grep_output_exactly_at_the_cap_is_kept_and_one_byte_more_is_cut() -> None:
    """TH3. The cap is a limit on the bytes of grep's output: the byte at the cap stays, the next one goes.

    Mutation: ``len(data) < cap`` (output at the cap is cut), ``len(data) <= cap + 1`` (a byte too
    many is kept), or a cut that splits a letter in the middle.
    """

    assert bench.cut_to_cap(b"a" * 100, 100) == ("a" * 100, False)
    assert bench.cut_to_cap(b"a" * 101, 100) == ("a" * 100 + "\n[output cut at 100 bytes]\n", True)
    assert bench.cut_to_cap(b"a" * 5000, None) == ("a" * 5000, False)
    text, cut = bench.cut_to_cap("é".encode() * 60, 99)
    assert cut is True and text == "é" * 49 + "\n[output cut at 99 bytes]\n"


# TH8: where a build may write ---------------------------------------------------------


def test_a_rebuild_never_deletes_the_run_directory_and_a_vault_cannot_sit_on_a_name_the_harness_keeps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH8. ``--data-dir`` is a folder of its own inside the run directory, never the directory or its parts.

    A rebuild deletes the vault folder, so a vault folder that is the run directory, the snapshot, the
    counter folder, the manifest or the marker would take the run with it.

    Mutation: let ``check_vault_location`` accept the run directory itself or a reserved name, or drop
    the check from ``build_vault``.
    """

    run_dir = tmp_path / "run"
    base = ["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS)]
    assert bench.main(base) == 0
    resolved = run_dir.resolve()
    vault = resolved / bench.VAULT_DIRNAME / bench.VAULT_FILENAME
    capsys.readouterr()
    for bad, message in (
        (resolved, "cannot be the run directory itself"),
        (resolved / bench.SNAPSHOT_DIRNAME, "keeps for itself"),
        (resolved / bench.SNAPSHOT_DIRNAME / "inner", "keeps for itself"),
        (resolved / bench.STATE_DIRNAME, "keeps for itself"),
        (resolved / bench.STATE_DIRNAME / "one", "keeps for itself"),
        (resolved / bench.MANIFEST_FILENAME, "keeps for itself"),
        (resolved / bench.RUN_MARKER, "keeps for itself"),
        (resolved / ".." / "elsewhere", "outside the run directory"),
    ):
        assert bench.main([*base, "--data-dir", str(bad), "--rebuild"]) == bench.EXIT_REFUSED, bad
        assert message in capsys.readouterr().err, bad
        assert (resolved / bench.RUN_MARKER).is_file() and (resolved / bench.MANIFEST_FILENAME).is_file(), bad
        assert (resolved / bench.SNAPSHOT_DIRNAME).is_dir() and vault.is_file(), bad
    with pytest.raises(bench.RunDirError, match="run directory itself"):
        bench.build_vault(
            corpus_dir=CORPUS,
            run_dir=resolved,
            data_dir=resolved,
            order="sorted",
            domain="project",
            sensitivity="internal",
            rebuild=True,
            repo=REPO_ROOT,
        )
    assert (resolved / bench.RUN_MARKER).is_file() and vault.is_file(), "a refused build deletes nothing"
    with pytest.raises(bench.RunDirError, match="run directory itself"):
        bench.check_vault_location(resolved, resolved)
    with pytest.raises(bench.RunDirError, match="keeps for itself"):
        bench.check_vault_location(resolved, resolved / bench.SNAPSHOT_DIRNAME / "x")
    assert bench.check_vault_location(resolved, resolved / "second-vault") == resolved / "second-vault"
    assert bench.main([*base, "--rebuild", "--order", "reverse"]) == 0
    assert _manifest(run_dir)["order"] == "reverse" and vault.is_file()


def test_labels_that_recall_would_hide_are_refused_at_build_and_the_ceiling_is_the_products(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH10. Both arms read the same text, so a file recall cannot return is never put in the grep snapshot.

    The control imports a file above the default ceiling by hand and shows that a default recall does
    not return it, which is the reason for the refusal. If the product ever changes that default, this
    test says so.

    Mutation: remove ``require_visible_labels`` from ``build_vault``, or shrink or widen the lists it
    reads from the checkout. A confidential corpus would build, with grep reading it and recall blind.
    """

    from alicebot_api import vnext_agent_control, vnext_memory_commit

    assert "confidential" not in vnext_agent_control.DEFAULT_AGENT_SENSITIVITY
    assert {"health", "legal"} <= set(vnext_memory_commit.SENSITIVE_DOMAINS)
    for number, flags in enumerate(
        (["--sensitivity", "confidential"], ["--sensitivity", "regulated"], ["--domain", "health"], ["--domain", "financial"])
    ):
        run_dir = tmp_path / f"refused-{number}"
        assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS), *flags]) == bench.EXIT_REFUSED
        assert not (run_dir / bench.VAULT_DIRNAME).exists() and not (run_dir / bench.MANIFEST_FILENAME).exists()
    err = capsys.readouterr().err
    assert "above what recall answers by default" in err and "sensitive domains" in err

    corpus_file = CORPUS / "tide-keeping.md"
    results: dict[str, int] = {}
    for sensitivity in ("private", "confidential"):
        data_dir = tmp_path / f"vault-{sensitivity}"
        data_dir.mkdir()
        db = bench.vault_path(data_dir)
        bench._import_one(db, corpus_file, domain="project", sensitivity=sensitivity)
        with bench.scoped_environment():
            text = bench.McpSession(db).recall("Who has the spare key to the brass cabinet?")
        results[sensitivity] = len(json.loads(text).get("sources", []))
    assert results == {"private": 1, "confidential": 0}
    for sensitivity in ("public", "internal", "private", "unknown"):
        run_dir = tmp_path / f"allowed-{sensitivity}"
        assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS), "--sensitivity", sensitivity]) == 0


def test_a_source_that_would_land_outside_the_snapshot_or_holds_no_text_is_refused(tmp_path: Path) -> None:
    """TH10. The grep snapshot is written only inside its own folder, from text the vault stored.

    One stored source is given a path outside the corpus and a relative path that climbs out of the
    snapshot folder. A second case removes the stored text.

    Mutation: drop the ``_is_inside`` check on the target in ``export_snapshot``, or the ``raw_text``
    check. A file would be written beside the snapshot, or an empty file would stand in for a source.
    """

    run_dir = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS)]) == 0
    db = run_dir / bench.VAULT_DIRNAME / bench.VAULT_FILENAME

    def edit(statement: str, *parameters: str) -> None:
        connection = sqlite3.connect(db)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    snapshot = tmp_path / "deep" / "inner" / "snapshot"
    edit(
        "UPDATE sources SET raw_path = ?, metadata_json = json_set(metadata_json, '$.relative_path', ?) "
        "WHERE rowid = (SELECT MIN(rowid) FROM sources)",
        "/somewhere/else/note.md",
        "../../../escape.md",
    )
    with pytest.raises(bench.BenchError, match="leaves the snapshot folder"):
        bench.export_snapshot(db, snapshot, corpus_root=CORPUS)
    assert not (tmp_path / "escape.md").exists() and not (tmp_path / "deep" / "escape.md").exists()
    edit("UPDATE sources SET metadata_json = json_remove(metadata_json, '$.raw_text')")
    with pytest.raises(bench.BenchError, match="holds no raw_text"):
        bench.export_snapshot(db, tmp_path / "other-snapshot", corpus_root=CORPUS)


def test_an_empty_or_unreadable_corpus_folder_is_a_refusal_and_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH10. A folder with no Markdown, a missing folder and an importer that returns nothing all stop the build.

    Mutation: remove the ``if not files`` check from ``read_corpus``, or the conversion of the importer's
    own refusal. The first would let an empty corpus build, the second would end in a traceback.
    """

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(bench.BenchError, match="cannot be read"):
        bench.read_corpus(empty)
    with pytest.raises(bench.BenchError, match="cannot be read"):
        bench.read_corpus(tmp_path / "missing")
    assert bench.main(["build", "--run-dir", str(tmp_path / "run"), "--corpus", str(empty)]) == bench.EXIT_REFUSED
    assert "cannot be read" in capsys.readouterr().err
    monkeypatch.setattr("alicebot_api.markdown_import._snapshot_markdown_source", lambda *args, **kwargs: (tmp_path, []))
    with pytest.raises(bench.BenchError, match="holds no Markdown files"):
        bench.read_corpus(tmp_path)


def test_a_shuffle_seed_changes_the_capture_order_and_the_same_seed_gives_it_again() -> None:
    """TH14. Different seeds give different orders, and the three orders of ``gates.json`` are all different.

    Mutation: ignore the seed in ``order_files`` (shuffle with a fixed seed), or accept a seed that is
    not a number. A seed of 0 or one fixed seed would give every order the same shuffle.
    """

    files = bench.read_corpus(CORPUS)[1]
    names = [item.relative_path for item in files]

    def order(spec: str) -> list[str]:
        return [item.relative_path for item in bench.order_files(files, spec)]

    shuffled = {seed: order(f"shuffle:{seed}") for seed in (0, 1, 2, 20261002)}
    assert len({tuple(value) for value in shuffled.values()}) == 4
    for value in shuffled.values():
        assert sorted(value) == names and value != names and value != list(reversed(names))
    assert order("shuffle:1") == shuffled[1]
    gates = bench.load_gates()
    assert gates.import_orders == ("sorted", "reverse", "shuffle:20261002")
    assert order(gates.import_orders[2]) == shuffled[20261002]
    assert order("sorted") == names and order("reverse") == list(reversed(names))
    for bad in ("shuffle:abc", "shuffle:", "shuffle", "sideways"):
        with pytest.raises(bench.BenchError):
            bench.order_files(files, bad)


# The session, the snapshot layout and the run directory ----------------------


def test_a_recall_that_comes_back_as_a_tool_error_stops_the_run_and_is_never_scored(run: Path) -> None:
    """TH1. An error answer is not an output: ``recall`` raises, and ``call`` says which kind it was.

    Mutation: return the error text from ``McpSession.recall`` instead of raising, or report an error
    result as a success in ``McpSession.call``. A failed recall would then be scored as a miss.
    """

    db = run / bench.VAULT_DIRNAME / bench.VAULT_FILENAME
    with bench.scoped_environment():
        session = bench.McpSession(db)
        is_error, text = session.call("alice_recall", {})
        assert is_error is True and text
        is_error, text = session.call("alice_recall", {"query": "spare key"})
        assert is_error is False and json.loads(text)["sources"]
        is_error, _ = session.call("no_such_tool", {})
        assert is_error is True
        assert json.loads(session.recall("spare key"))["sources"]
        session.call = lambda name, arguments: (True, "boom")  # type: ignore[method-assign]
        with pytest.raises(bench.BenchError, match="alice_recall returned an error: boom"):
            session.recall("spare key")


def test_files_with_one_name_in_two_folders_keep_their_folders_in_the_snapshot_and_in_grep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH10. The grep snapshot names each file by its path in the corpus, so two ``notes.md`` files stay two.

    Mutation: name a snapshot file by its file name alone in ``export_snapshot``. The second would
    overwrite the first and grep would read one file where recall holds two sources.
    """

    corpus = tmp_path / "corpus"
    (corpus / "north").mkdir(parents=True)
    (corpus / "south").mkdir()
    (corpus / "north" / "notes.md").write_text("# North\n\nThe north beacon is green.\n", encoding="utf-8")
    (corpus / "south" / "notes.md").write_text("# South\n\nThe south beacon is amber.\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(corpus)]) == 0
    snapshot = bench.read_snapshot(run_dir / bench.SNAPSHOT_DIRNAME)
    assert sorted(snapshot) == ["north/notes.md", "south/notes.md"]
    assert "green" in snapshot["north/notes.md"] and "amber" in snapshot["south/notes.md"]
    assert _manifest(run_dir)["capture_order"] == ["north/notes.md", "south/notes.md"]
    capsys.readouterr()
    state = _state(run_dir, tmp_path.name, "grep")
    assert bench.main(["search", "--arm", "grep", "--run-dir", str(run_dir), "--state-dir", str(state), "--pattern", "beacon"]) == 0
    assert capsys.readouterr().out == "north/notes.md:The north beacon is green.\nsouth/notes.md:The south beacon is amber.\n"


def test_a_run_directory_that_is_a_file_is_refused_and_a_missing_one_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TH8. ``--run-dir`` must be a directory this harness made.

    Mutation: drop the not-a-directory check in ``prepare_run_dir`` or the marker check in
    ``require_run_dir``. A build would try to make folders inside a file, and a read would open
    whatever sits at the path.
    """

    a_file = tmp_path / "plain-file"
    a_file.write_text("not a directory", encoding="utf-8")
    assert bench.main(["build", "--run-dir", str(a_file), "--corpus", str(CORPUS)]) == bench.EXIT_REFUSED
    assert "not a directory" in capsys.readouterr().err
    assert a_file.read_text() == "not a directory"
    missing = tmp_path / "missing"
    assert bench.main(["recall", "--run-dir", str(missing), "--query", "key"]) == bench.EXIT_REFUSED
    assert "not made by this harness" in capsys.readouterr().err
    assert not missing.exists()


def test_the_pinned_settings_are_the_tools_own_defaults_and_the_ones_gates_json_names() -> None:
    """TH3. The Alice arm is pinned to what the tool does when it is not told otherwise.

    The tool says its defaults in the descriptions of its arguments, and ``gates.json`` records them.

    Mutation: change a value of ``PINNED_RECALL_DEFAULTS`` (``limit`` 7, ``context_depth`` ``medium``,
    ``include_sources`` ``False``). The arm would then be pinned to something the product does not do
    by default, and the claim would name a setting nobody runs.
    """

    gates = bench.load_gates()
    assert dict(bench.PINNED_RECALL_DEFAULTS) == gates.data["search"]["recall_defaults"]
    with bench.scoped_environment():
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder) / "run"
            assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS)]) == 0
            tools = bench.McpSession(run_dir / bench.VAULT_DIRNAME / bench.VAULT_FILENAME).tools_list()
    properties = next(tool for tool in tools if tool["name"] == "alice_recall")["inputSchema"]["properties"]
    assert f"Defaults to {bench.PINNED_RECALL_DEFAULTS['limit']}." in properties["limit"]["description"]
    assert f"'{bench.PINNED_RECALL_DEFAULTS['context_depth']}' (default)" in properties["context_depth"]["description"]
    assert "Defaults to true" in properties["include_sources"]["description"]
    assert bench.PINNED_RECALL_DEFAULTS["include_sources"] is True


def test_each_grep_letter_is_allowed_or_refused_on_purpose_and_bad_options_are_named() -> None:
    """TH3. The flags an agent may add are a short list of letters, and nothing outside it gets through.

    Mutation: add a letter to ``_GREP_LETTERS`` (``f`` reads patterns from a file, ``P`` changes the
    regex dialect), remove one, or let a count option take something that is not a number.
    """

    allowed = set("inwxEFlLcohHvsIrR")
    for letter in string.ascii_letters:
        if letter in allowed:
            assert bench.validate_grep_options(f"-{letter}") == [f"-{letter}"], letter
        else:
            with pytest.raises(bench.GrepOptionError):
                bench.validate_grep_options(f"-{letter}")
    assert bench.validate_grep_options("") == []
    assert bench.validate_grep_options("-i -C 3 -m 5 -A2 -B1") == ["-i", "-C", "3", "-m", "5", "-A2", "-B1"]
    for bad in ("-C x", "-m", "-A -n", "-i 'unclosed", "--color", "-", "word", "-n -n -f", "-iZ", "-C2x"):
        with pytest.raises(bench.GrepOptionError):
            bench.validate_grep_options(bad)


def test_the_manifest_counts_duplicates_chunks_files_and_hashes_what_it_describes(tmp_path: Path) -> None:
    """TH9. A corpus with two identical files builds one source and says one was a duplicate.

    The manifest also holds the chunk count of the vault, the number of corpus files and the hash of
    the snapshot that grep reads, each checked here against the vault or the snapshot itself.

    Mutation: count every file as a duplicate or none, take ``chunks`` from another table, or hash
    something other than the snapshot.
    """

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "first.md").write_text("# Same\n\nThe same text twice over.\n", encoding="utf-8")
    (corpus / "second.md").write_text("# Same\n\nThe same text twice over.\n", encoding="utf-8")
    sections = "".join(f"## Part {number}\n\nThis is part {number} of another text entirely.\n\n" for number in range(1, 6))
    (corpus / "third.md").write_text("# Other\n\n" + sections, encoding="utf-8")
    run_dir = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(corpus)]) == 0
    manifest = _manifest(run_dir)
    assert (manifest["corpus_file_count"], manifest["sources"], manifest["duplicates"]) == (3, 2, 1)
    db = run_dir / bench.VAULT_DIRNAME / bench.VAULT_FILENAME
    assert manifest["chunks"] == bench.vault_row_counts(db)["source_chunks"] > manifest["sources"]
    snapshot = bench.read_snapshot(run_dir / bench.SNAPSHOT_DIRNAME)
    assert manifest["snapshot_hash"] == bench.snapshot_hash(snapshot)
    assert sorted(snapshot) == ["first.md", "third.md"], "the duplicate has no source, so it is not in the grep snapshot"
    assert manifest["corpus_hash"] == bench.corpus_hash(bench.read_corpus(corpus)[1])
    assert manifest["harness_sha256"] == hashlib.sha256(BENCH_SCRIPT.read_bytes()).hexdigest()


def test_a_source_the_vault_has_deleted_is_not_in_the_grep_snapshot(tmp_path: Path) -> None:
    """TH10. Recall never returns a deleted source, so grep must not read one either.

    Mutation: drop the ``deleted_at IS NULL`` test from ``read_vault_sources``.
    """

    run_dir = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run_dir), "--corpus", str(CORPUS)]) == 0
    db = run_dir / bench.VAULT_DIRNAME / bench.VAULT_FILENAME
    before = [source["external_id"] for source in bench.read_vault_sources(db)]
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE sources SET deleted_at = '2026-10-02T00:00:00Z' WHERE rowid = (SELECT MIN(rowid) FROM sources)"
        )
        connection.commit()
    finally:
        connection.close()
    after = [source["external_id"] for source in bench.read_vault_sources(db)]
    assert len(before) == 8 and len(after) == 7 and set(after) < set(before)
    snapshot = tmp_path / "snapshot"
    assert len(bench.export_snapshot(db, snapshot, corpus_root=CORPUS)) == 7
    assert len(list(snapshot.rglob("*.md"))) == 7
