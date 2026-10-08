"""Replay exact guard removals in a disposable copy of an immutable commit.

Run with the normal development Python and role-separated synthetic test DB
environment. The JSON manifest lists exact source spans, replacement text,
one-based lines, file digests and expected failing tests. No checkout is edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    cases = json.loads((repository / "scripts/derived_label_mutations.json").read_text())["cases"]
    selected = [case for case in cases if not args.only or case["name"] in args.only]
    if not selected:
        parser.error("no selected mutation cases")
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="alice-label-mutants-") as directory:
        root = Path(directory).resolve()
        archive = root / "source.tar"
        with archive.open("wb") as destination:
            subprocess.run(["git", "-C", str(repository), "archive", revision], stdout=destination, check=True)
        checkout = root / "checkout"
        checkout.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(checkout, filter="data")
        env = {**os.environ, "PYTHONPATH": str(checkout / "apps/api/src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for case in selected:
            originals = {}
            for edit in case["edits"]:
                path = (checkout / edit["file"]).resolve()
                if not path.is_relative_to(checkout):
                    raise ValueError("mutation path outside disposable checkout")
                raw = path.read_bytes()
                text = raw.decode()
                if hashlib.sha256(raw).hexdigest() != edit["sha256"]:
                    raise ValueError("manifest digest mismatch: " + edit["file"])
                if text.count(edit["before"]) != 1 or text[:text.index(edit["before"])].count("\n") + 1 != edit["line"]:
                    raise ValueError("manifest source span mismatch: " + edit["file"])
                originals[path] = raw
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *case["pytest"]]
            baseline = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=180)
            (args.output / (case["name"] + "-baseline.log")).write_text(baseline.stdout + baseline.stderr)
            mutant = None
            try:
                if baseline.returncode == 0:
                    for edit in case["edits"]:
                        path = checkout / edit["file"]
                        path.write_text(path.read_text().replace(edit["before"], edit["after"], 1))
                    mutant = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=180)
                    (args.output / (case["name"] + "-mutant.log")).write_text(mutant.stdout + mutant.stderr)
            finally:
                for path, raw in originals.items():
                    path.write_bytes(raw)
            output = (mutant.stdout + mutant.stderr) if mutant else ""
            killed = baseline.returncode == 0 and mutant is not None and mutant.returncode == 1 and any(
                "FAILED " in line and case["expected_failure"] in line for line in output.splitlines()
            )
            result = {"name": case["name"], "revision": revision, "baseline_exit": baseline.returncode,
                      "mutant_exit": mutant.returncode if mutant else None, "killed": killed,
                      "expected_failure": case["expected_failure"], "edits": case["edits"], "command": command}
            results.append(result)
            print(json.dumps({key: result[key] for key in ("name", "baseline_exit", "mutant_exit", "killed")}), flush=True)
            (args.output / "summary.json").write_text(json.dumps({"revision": revision, "results": results}, indent=2) + "\n")
    return 0 if all(result["killed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
