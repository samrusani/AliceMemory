"""The benchmark measures isolated commits and does not edit its checkout."""

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.performance import lessons_brief_benchmark as bench

SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def repository_for_archive_replay(tmp_path_factory, monkeypatch):
    source = os.environ.get("BENCH_TEST_SOURCE")
    if source:
        clone = tmp_path_factory.mktemp("benchmark-repo") / "checkout"
        subprocess.run(["git", "clone", "--quiet", "--single-branch", "--no-local",
                        Path(source).resolve().as_uri(), str(clone)], check=True)
        monkeypatch.setattr(bench, "ROOT", clone)


def snapshot():
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=bench.ROOT
    ).split(b"\0")
    return {name: hashlib.sha256((bench.ROOT / os.fsdecode(name)).read_bytes()).hexdigest()
            for name in names if name and (bench.ROOT / os.fsdecode(name)).is_file()}


def trees_at(root, base="HEAD"):
    trees = {side: root / side for side in ("base", "head")}
    bench.extract_tree(base, trees["base"])
    bench.extract_tree("HEAD", trees["head"])
    return trees


def mark(tree, text):
    path = tree / "apps/api/src/alicebot_api/onramp.py"
    source = path.read_text()
    path.write_text(source.replace("from __future__ import annotations\n",
                                   "from __future__ import annotations\n" + text + "\n", 1))


def test_each_side_imports_its_own_tree_and_marker(tmp_path, monkeypatch):
    """Guard: select the measured side's tree for each subprocess."""
    trees = trees_at(tmp_path)
    for side, tree in trees.items():
        mark(tree, f"print('marker-{side}')")
    observed = []
    real = bench.run_sample

    def record(tree, *args):
        value = real(tree, *args)
        observed.append((tree, value))
        return value

    monkeypatch.setattr(bench, "run_sample", record)
    result = bench.run_benchmark(trees, tmp_path, samples=2, rows_count=16, shapes=("none",))[0]
    assert len(observed) == 6
    assert Path(result["imports"]["base"]).is_relative_to(trees["base"])
    assert Path(result["imports"]["head"]).is_relative_to(trees["head"])
    for tree, (_, imported, output) in observed:
        assert Path(imported).is_relative_to(tree)
        assert f"marker-{tree.name}" in output
        assert result["imports"][tree.name] == imported


def test_run_leaves_checkout_bytes_unchanged(tmp_path):
    """Guard: measure snapshots without writing the checkout."""
    trees = trees_at(tmp_path)
    before = snapshot()
    bench.run_benchmark(trees, tmp_path, samples=2, rows_count=16, shapes=("none",))
    assert snapshot() == before


def test_sigkill_during_sample_leaves_checkout_bytes_unchanged(tmp_path):
    """Guard: never write a revision into the checkout even during a sample."""
    trees = trees_at(tmp_path)
    started = tmp_path / "sample-started"
    mark(trees["base"], f"open({str(started)!r}, 'w').close(); __import__('time').sleep(30)")
    before = snapshot()
    code = ("from pathlib import Path; from tests.performance import lessons_brief_benchmark as b; "
            f"b.ROOT=Path({str(bench.ROOT)!r}); r=Path({str(tmp_path)!r}); "
            "b.run_benchmark({'base':r/'base','head':r/'head'},r,2,16,('none',))")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(SOURCE / "apps/api/src"), str(SOURCE))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-c", code], cwd=bench.ROOT, env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert started.exists(), "sample did not start"
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        assert process.returncode == -signal.SIGKILL
        assert snapshot() == before
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_old_base_without_lessons_file_is_measured_or_names_the_side(tmp_path):
    """Guard: extract the whole old tree rather than asking Git for today's files."""
    if subprocess.run(["git", "cat-file", "-e", "7813ff91^{commit}"], cwd=bench.ROOT,
                      capture_output=True).returncode:
        pytest.skip("revision 7813ff91 is not available in this clone")
    trees = trees_at(tmp_path, "7813ff91")
    assert not (trees["base"] / "apps/api/src/alicebot_api/mcp/lessons.py").exists()
    try:
        result = bench.run_benchmark(trees, tmp_path, samples=2, rows_count=16, shapes=("none",))
    except bench.SideError as exc:
        assert exc.side == "base"
        assert str(exc).strip()
        assert "fatal: path" not in str(exc)
    else:
        assert Path(result[0]["imports"]["base"]).is_relative_to(trees["base"])


def test_cli_keeps_named_revisions_and_import_paths(tmp_path, monkeypatch, capsys):
    """Guard: retain both resolved revision ids and measured import paths in output."""
    import json

    if subprocess.run(["git", "cat-file", "-e", "HEAD^"], cwd=bench.ROOT, capture_output=True).returncode:
        pytest.skip("parent of HEAD is not available in this clone")
    revisions = {"base": subprocess.check_output(["git", "rev-parse", "HEAD^"], cwd=bench.ROOT, text=True).strip(),
                 "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=bench.ROOT, text=True).strip()}

    def fake_run(trees, root, samples):
        return [{"shape": "none", "samples": samples, "imports": {s: str(t / "apps/api/src/alicebot_api")
                                                                     for s, t in trees.items()},
                 "measurements": {"base": [1.0], "head": [2.0]}, "results": {}}]

    monkeypatch.setattr(bench, "run_benchmark", fake_run)
    monkeypatch.setattr(sys, "argv", ["bench", "--base", "HEAD^", "--head", "HEAD", "--work-dir", str(tmp_path),
                                     "--keep"])
    assert bench.main() == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    record = records[-1]
    assert record["revisions"] == revisions
    assert Path(record["imports"]["base"]).is_relative_to(tmp_path)
    assert Path(record["imports"]["head"]).is_relative_to(tmp_path)
    saved = json.loads((next(tmp_path.iterdir()) / "none.json").read_text())
    assert saved["revisions"] == revisions
    assert saved["imports"] == record["imports"]


@pytest.mark.parametrize("interruption", [signal.SIGINT, signal.SIGTERM])
def test_interruption_removes_trees_and_fixtures(tmp_path, interruption):
    """Guard: clean the common temporary directory after an interrupt."""
    started = tmp_path / "started"
    code = ("import sys,time; from pathlib import Path; "
            "from tests.performance import lessons_brief_benchmark as b; "
            f"b.ROOT=Path({str(bench.ROOT)!r}); p=Path({str(started)!r}); "
            "defn='def run(*a):\\n p.write_text(\"started\")\\n time.sleep(60)'; exec(defn); "
            "b.run_benchmark=run; "
            f"sys.argv=['bench','--base','HEAD','--work-dir',{str(tmp_path)!r}]; "
            "sys.exit(b.main())")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(SOURCE / "apps/api/src"), str(SOURCE))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-c", code], cwd=bench.ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert started.exists(), "benchmark did not start"
        process.send_signal(interruption)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
        assert not list(tmp_path.glob("lessons-brief-bench-*"))
        assert '"shape"' not in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_failed_side_produces_no_result_for_the_pair(tmp_path, monkeypatch, capsys):
    """Guard: name the failing side and buffer results until both sides finish."""
    import json

    monkeypatch.setattr(bench, "seed", lambda *args: ("user", "key"))

    def sample(tree, path, *args):
        if tree.name == "base" and path.name == "published.db":
            raise RuntimeError("synthetic brief failure")
        return 1.0, str(tree / "apps/api/src/alicebot_api"), ""

    monkeypatch.setattr(bench, "run_sample", sample)
    monkeypatch.setattr(sys, "argv", ["bench", "--base", "HEAD", "--work-dir", str(tmp_path)])
    assert bench.main() == 1
    captured = capsys.readouterr()
    assert all("shape" not in json.loads(line) for line in captured.out.splitlines())
    error = json.loads(captured.err)
    assert error["side"] == "base"
    assert "synthetic brief failure" in error["error"]
    assert not list(tmp_path.iterdir())


def test_cli_requires_forty_samples(monkeypatch, capsys):
    """Guard: reject fewer than 40 command-line samples."""
    monkeypatch.setattr(sys, "argv", ["bench", "--samples", "39"])
    with pytest.raises(SystemExit) as exc:
        bench.main()
    assert exc.value.code == 2
    assert "at least 40 samples" in capsys.readouterr().err
