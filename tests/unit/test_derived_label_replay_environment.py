"""The replay refuses misleading archives and reports an unrun mutant."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

import scripts.verify_derived_label_mutations as replay


def _exercise(monkeypatch, tmp_path, *, ignored=False, source=None):
    repo = tmp_path / 'repository'
    (repo / 'scripts').mkdir(parents=True)
    data = b'guard\n'
    case = {'name': 'fixture', 'edits': [{'file': 'CURRENT_STATE.md', 'line': 1,
            'before': 'guard', 'after': 'changed', 'sha256': hashlib.sha256(data).hexdigest()}],
            'pytest': ['tests/unit/test_fixture.py::test_fixture'], 'expected_failure': 'test_fixture'}
    (repo / 'scripts/derived_label_mutations.json').write_text(json.dumps({'cases': [case]}))
    outer = tmp_path / 'outer'
    outer.mkdir()
    subprocess.run(['git', 'init', '-q', str(outer)], check=True)
    (outer / '.gitignore').write_text('ignored/\n')
    temp = outer / ('ignored' if ignored else 'visible')
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    monkeypatch.setattr(replay, '__file__', str(repo / 'scripts/verify_derived_label_mutations.py'))
    monkeypatch.setattr(sys, 'argv', ['verify', '--output', str(tmp_path / 'output')])
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: 'revision\n')
    if source is None:
        monkeypatch.delenv('BENCH_TEST_SOURCE', raising=False)
    else:
        monkeypatch.setenv('BENCH_TEST_SOURCE', source)
    real_run = subprocess.run
    seen = []

    def run(command, **kwargs):
        if command[0] == 'git' and 'archive' in command:
            with tarfile.open(fileobj=kwargs['stdout'], mode='w') as archive:
                info = tarfile.TarInfo('CURRENT_STATE.md')
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            return SimpleNamespace(returncode=0)
        if command[0] == sys.executable:
            seen.append(kwargs['env']['BENCH_TEST_SOURCE'])
            return SimpleNamespace(returncode=1, stdout='baseline failed\n', stderr='')
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    return repo, seen


def test_ignored_archive_stops_before_any_case(monkeypatch, tmp_path, capsys):
    """Guard: removing the ignored.returncode check starts the case in an ignored archive."""
    _, seen = _exercise(monkeypatch, tmp_path, ignored=True)
    with pytest.raises(SystemExit) as error:
        replay.main()
    assert error.value.code == 2
    assert not seen
    assert 'archived files are ignored by an enclosing Git repository' in capsys.readouterr().err
    assert not (tmp_path / 'output').exists()


def test_unset_benchmark_source_defaults_to_the_repository(monkeypatch, tmp_path):
    """Guard: removing env.setdefault loses the repository source on the baseline."""
    repo, seen = _exercise(monkeypatch, tmp_path)
    assert replay.main() == 1
    assert seen == [str(repo)]


def test_explicit_benchmark_source_is_preserved(monkeypatch, tmp_path):
    """Guard: assigning BENCH_TEST_SOURCE unconditionally overwrites the caller's source."""
    _, seen = _exercise(monkeypatch, tmp_path, source='explicit-source')
    assert replay.main() == 1
    assert seen == ['explicit-source']


def test_failed_baseline_is_reported_without_running_the_mutant(monkeypatch, tmp_path, capsys):
    """Guard: changing the baseline check to run mutants after failure violates the report."""
    _, seen = _exercise(monkeypatch, tmp_path)
    assert replay.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert (result['baseline_exit'], result['mutant_exit'], result['killed']) == (1, None, False)
    assert len(seen) == 1
    assert not (tmp_path / 'output/fixture-mutant.log').exists()


def test_failed_baseline_removes_a_previous_mutant_log(monkeypatch, tmp_path):
    """Guard: removing the mutant-log unlink leaves an old result beside the failed baseline."""
    _exercise(monkeypatch, tmp_path)
    (tmp_path / 'output').mkdir()
    (tmp_path / 'output/fixture-mutant.log').write_text('old result')
    assert replay.main() == 1
    assert not (tmp_path / 'output/fixture-mutant.log').exists()
