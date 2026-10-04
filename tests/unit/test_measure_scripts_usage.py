"""The usage text of the two measurement scripts is a command someone will paste.

Until 2026-10-04 both usage texts named a scratch folder. The hygiene test refuses
scratch paths in code, so the text now shows a relative ``--data-dir``.
A relative folder only helps if the script accepts one, and
``scripts/measure_project_view.py`` did not: ``build`` handed the relative
scratch repository path ``<data-dir>-repo`` to the project detector, which
resolved nothing, so the documented command stopped with "the scratch repository
did not resolve".

This test reads the ``--data-dir`` value out of each script's own usage text and
runs ``build`` then ``measure`` with exactly that value from an empty working
folder, at a tiny size.

Mutation: remove the ``data_dir = data_dir.resolve()`` line in ``build_vault`` of
``scripts/measure_project_view.py`` (the project view case fails at ``build``), or
change either usage text to a value that is not a working folder name, such as
``--data-dir``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# script, the sizes that keep the run to a few seconds, and what ``build`` leaves beside or inside the vault
_SCRIPTS = {
    "scripts/measure_project_view.py": (("--notes", "60"), ("measure_project_view.json",)),
    "scripts/measure_recall_source_lookup.py": (("--sources", "20", "--memories", "20", "--events", "60"), ()),
}


def _documented_data_dir(script: str) -> str:
    doc = ast.get_docstring(ast.parse((REPO_ROOT / script).read_text(encoding="utf-8"))) or ""
    values = set(re.findall(r"--data-dir\s+(\S+)", doc))
    assert len(values) == 1, f"{script}: the usage text must name one --data-dir value, found {sorted(values)}"
    (value,) = values
    assert re.fullmatch(r"[\w./-]+", value) and not value.startswith("-"), f"{script}: {value!r} is not a folder name"
    return value


def _run(script: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("script", sorted(_SCRIPTS))
def test_the_documented_command_builds_and_measures_from_an_empty_folder(script: str, tmp_path: Path) -> None:
    sizes, meta_files = _SCRIPTS[script]
    data_dir = _documented_data_dir(script)

    built = _run(script, tmp_path, "build", "--data-dir", data_dir, *sizes)
    assert built.returncode == 0, f"build with {data_dir!r} failed: {built.stderr or built.stdout}"
    assert json.loads(built.stdout), "build prints the vault it made"
    assert (tmp_path / data_dir / "memory.db").is_file()
    for name in meta_files:
        assert (tmp_path / data_dir / name).is_file(), name
        recorded = json.loads((tmp_path / data_dir / name).read_text(encoding="utf-8"))
        # The scratch repository is recorded as an absolute path, so measure finds it from any folder.
        assert Path(recorded["repo"]).is_absolute()
        assert (Path(recorded["repo"]) / ".git" / "HEAD").is_file()

    measured = _run(script, tmp_path, "measure", "--data-dir", data_dir, "--runs", "1", "--label", "usage-check")
    assert measured.returncode == 0, f"measure with {data_dir!r} failed: {measured.stderr or measured.stdout}"
    assert json.loads(measured.stdout)["label"] == "usage-check"
