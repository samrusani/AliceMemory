"""The combine step of the sharded unit tests fails closed when a shard left no coverage data.

The unit tests run as parallel shards and each writes one coverage data file. The combined
data is then held to the same coverage threshold the single job used. A shard that wrote
nothing would only lower the combined number, so the combine step names it and fails
instead of trusting whatever is left.
"""

from __future__ import annotations

from pathlib import Path

from coverage import CoverageData
import pytest

import scripts.combine_python_coverage as combine


def _write_shard(path: Path, arcs: dict[str, list[tuple[int, int]]] | None) -> None:
    """Write a real coverage data file; ``None`` writes a valid file that measured nothing."""
    data = CoverageData(basename=str(path))
    if arcs is None:
        data.add_arcs({})
    else:
        data.add_arcs(arcs)
    data.write()


def _all_shards(data_dir: Path) -> None:
    _write_shard(data_dir / "shard-1.coverage", {"/src/a.py": [(-1, 1), (1, 2), (2, -1)]})
    _write_shard(data_dir / "shard-2.coverage", {"/src/a.py": [(-1, 1), (1, 3), (3, -1)], "/src/b.py": [(-1, 1), (1, -1)]})
    _write_shard(data_dir / "shard-3.coverage", {"/src/c.py": [(-1, 5), (5, -1)]})


def _measured(path: Path) -> tuple[set[str], set[tuple[int, int]]]:
    data = CoverageData(basename=str(path))
    data.read()
    return set(data.measured_files()), set(data.arcs("/src/a.py") or [])


def _run(tmp_path: Path, *, shards: int = 3) -> int:
    return combine.main(["--data-dir", str(tmp_path), "--shards", str(shards), "--output", str(tmp_path / "combined")])


def test_every_shard_present_combines_into_the_union_of_their_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The combined data holds every file and every arc of every shard.

    Mutation: combine only the first shard file, or pass the output path as an input.
    """

    _all_shards(tmp_path)

    assert _run(tmp_path) == 0

    files, arcs = _measured(tmp_path / "combined")
    assert files == {"/src/a.py", "/src/b.py", "/src/c.py"}
    assert arcs == {(-1, 1), (1, 2), (2, -1), (1, 3), (3, -1)}
    assert "Python shard coverage combine: PASS (3 shards" in capsys.readouterr().out


def test_a_missing_shard_file_fails_the_combine_and_names_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A shard whose data never arrived fails the combine, whatever the others hold.

    Mutation: drop the ``is_file`` check (the size check then raises on the missing file),
    or let ``shard_problems`` return no problem for a missing file.
    """

    _all_shards(tmp_path)
    (tmp_path / "shard-2.coverage").unlink()

    assert _run(tmp_path) == 1

    out = capsys.readouterr().out
    assert "Python shard coverage combine: FAIL" in out
    assert f"missing coverage data: {tmp_path / 'shard-2.coverage'}" in out
    assert not (tmp_path / "combined").exists()


def test_a_zero_byte_shard_file_fails_the_combine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A shard that left an empty file fails with the empty-file message.

    Mutation: drop the zero-size check (the file is then read as an empty database and the
    message changes to the measured-nothing one), or treat a zero size as present.
    """

    _all_shards(tmp_path)
    (tmp_path / "shard-3.coverage").write_bytes(b"")

    assert _run(tmp_path) == 1

    assert f"empty coverage data: {tmp_path / 'shard-3.coverage'}" in capsys.readouterr().out
    assert not (tmp_path / "combined").exists()


def test_a_shard_file_that_measured_no_source_file_fails_the_combine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A well-formed data file with nothing measured is a shard that wrote no data.

    Mutation: drop the measured-files check (the combine then passes with two shards of data).
    """

    _all_shards(tmp_path)
    _write_shard(tmp_path / "shard-1.coverage", None)

    assert _run(tmp_path) == 1

    out = capsys.readouterr().out
    assert f"coverage data measured no source files: {tmp_path / 'shard-1.coverage'}" in out
    assert not (tmp_path / "combined").exists()


def test_an_unreadable_shard_file_fails_the_combine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file that is not a coverage database fails with the unreadable message.

    Mutation: drop the read error handling (the garbage then raises out of the script), or
    drop ``CoverageException`` from the handled errors.
    """

    _all_shards(tmp_path)
    (tmp_path / "shard-2.coverage").write_bytes(b"this is not a coverage database")

    assert _run(tmp_path) == 1

    assert f"unreadable coverage data: {tmp_path / 'shard-2.coverage'}" in capsys.readouterr().out


def test_every_problem_is_reported_not_only_the_first(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two bad shards are both named, so one rerun does not hide the second.

    Mutation: return after the first problem.
    """

    _all_shards(tmp_path)
    (tmp_path / "shard-1.coverage").unlink()
    (tmp_path / "shard-3.coverage").write_bytes(b"")

    assert _run(tmp_path) == 1

    out = capsys.readouterr().out
    assert "shard-1.coverage" in out and "shard-3.coverage" in out


def test_data_that_cannot_be_combined_fails_instead_of_raising(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Line data and arc data cannot be merged, and the script reports that as a failure.

    Mutation: drop the handler around ``combine_shards``.
    """

    _all_shards(tmp_path)
    lines_only = CoverageData(basename=str(tmp_path / "shard-3.coverage"))
    lines_only.erase()
    lines_only = CoverageData(basename=str(tmp_path / "shard-3.coverage"))
    lines_only.add_lines({"/src/c.py": [5]})
    lines_only.write()

    assert _run(tmp_path) == 1

    assert "could not combine" in capsys.readouterr().out


def test_the_combined_file_replaces_stale_data_instead_of_adding_to_it(tmp_path: Path) -> None:
    """Data already stored at the output path does not carry into the result.

    An old run's data in the output file would add files and lines the shards did not run.

    Mutation: read the existing output first (``CoverageData.read``) and add the shard data to it.
    """

    _all_shards(tmp_path)
    _write_shard(tmp_path / "combined", {"/src/stale.py": [(-1, 1), (1, -1)]})

    assert _run(tmp_path) == 0

    files, _arcs = _measured(tmp_path / "combined")
    assert "/src/stale.py" not in files
    assert files == {"/src/a.py", "/src/b.py", "/src/c.py"}


def test_the_expected_files_follow_the_shard_count_and_the_shared_name(tmp_path: Path) -> None:
    """The names are the ones the workflow writes, in shard order, one per shard.

    Mutation: count shards from 0, or change the template (the workflow test pins the same name).
    """

    assert combine.DATA_FILE_TEMPLATE == "shard-{number}.coverage"
    assert combine.shard_data_files(tmp_path, 3) == [
        tmp_path / "shard-1.coverage",
        tmp_path / "shard-2.coverage",
        tmp_path / "shard-3.coverage",
    ]
    assert combine.shard_data_files(tmp_path, 1) == [tmp_path / "shard-1.coverage"]


def test_a_shard_count_below_one_is_refused(tmp_path: Path) -> None:
    """Zero shards would combine nothing and pass.

    Mutation: drop the ``--shards`` check.
    """

    with pytest.raises(SystemExit) as raised:
        combine.main(["--data-dir", str(tmp_path), "--shards", "0"])

    assert raised.value.code == 2
