"""The publish workflow's lean jobs run release scripts with no package installed.

``publish-pypi.yml`` installs the project in one job only. Every other job runs
``python scripts/X.py`` on the runner's bare interpreter: no ``alicebot_api``,
no packages outside the standard library. v0.19.1 was tagged and its publish run failed at the
draft readback because ``release_check.py`` imported ``alicebot_api`` inside a
function. Nothing in CI ran that script without the package installed, so
nothing noticed. The workflow runs from the tag, so a tagged commit with that
import can never publish and the number is burned.

Three layers here, none of which needs editing when a job or step is added:

* a structural guard that reads the job list from the workflow YAML, takes
  every job that does not install the project, collects the scripts it runs,
  and proves by AST that each script's whole import closure is the standard
  library or a sibling ``scripts/`` module, following function-level and
  conditional imports;
* a proof, by call graph from ``main()``, that the one installed-only import
  that is left cannot be reached by an invocation a lean job makes;
* execution tests that run the scripts under ``python -I -S`` (isolated, no
  site-packages) on the path that failed.

Mutations that must fail this file, each alone:

* put the import of ``CLAUDE_PLUGIN_ID`` from the package back inside
  ``_marketplace_issues`` in ``scripts/release_check.py``: the structural guard
  and the bare execution tests fail;
* add ``import yaml`` (or any import outside the standard library) at the top or inside a
  function of any script a lean job runs, or add a lean job step that runs
  ``python scripts/run_phase5_ops_evidence.py``: the structural guard fails;
* call ``validate_semantic_eval_report`` from ``validate_metadata`` so a lean
  invocation reaches the installed-only import: the reachability test fails;
* change the value of ``CLAUDE_PLUGIN_ID`` in ``scripts/release_check.py``:
  ``test_the_release_check_plugin_id_equals_the_installed_constant`` in
  ``tests/unit/test_claude_code_plugin.py`` fails. That test lives there, not
  here, because it imports the package's ``host_install`` module and the real-host CI
  path filter lists every test that does.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest
import yaml  # dev extra, used here as a test oracle only

from scripts import (
    decode_github_release_body,
    normalize_sdist,
    release_check,
    render_release_body,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-pypi.yml"
STDLIB = frozenset(sys.stdlib_module_names)

# The only imports a lean job's script may make that are not standard library or
# a sibling script. Key: (script, outermost function containing the import).
# Value: the exact module imported. An entry is allowed only while the function
# is unreachable from main() when the --semantic-eval-* branches are skipped,
# and a stale entry (no such import any more) fails the test.
INSTALLED_ONLY_IMPORTS: dict[tuple[str, str], frozenset[str]] = {
    ("release_check", "_generator_release_contract"): frozenset(
        {"alicebot_api.vnext_evals"}
    ),
}


# --- workflow: which jobs are lean, which scripts they run -------------------

_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b(?P<rest>[^\n;&|]*)")
_SCRIPT_PATH = re.compile(r"(?<![\w./-])scripts/(?P<name>[A-Za-z_]\w*)\.py\b")
_SCRIPT_MODULE = re.compile(r"-m\s+scripts\.(?P<name>[A-Za-z_]\w*)\b")


def _installs_the_package(run_text: str) -> bool:
    """True when a run step installs this project (editable, path, extras, wheel, sdist)."""

    flattened = run_text.replace("\\\n", " ")
    for match in _PIP_INSTALL.finditer(flattened):
        rest = match.group("rest")
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()
        for token in tokens:
            if token in {"-e", "--editable", "."} or token.startswith(
                ("--editable=", ".[", "./")
            ):
                return True
            if re.match(r"alice[-_]memory\b", token, flags=re.IGNORECASE):
                return True
            if token.endswith((".whl", ".tar.gz")):
                return True
    return False


def classify_jobs(
    workflow_text: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, list[str]]]:
    """Return (installing jobs, lean jobs, lean jobs' run text), each keyed by job id.

    The first two map to the set of ``scripts/X.py`` module names the job runs.
    """

    loaded = yaml.safe_load(workflow_text)
    jobs = loaded["jobs"]
    installing: dict[str, set[str]] = {}
    lean: dict[str, set[str]] = {}
    lean_runs: dict[str, list[str]] = {}
    for job_id, job in jobs.items():
        runs = [
            step["run"]
            for step in job.get("steps") or []
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]
        scripts = {
            match.group("name")
            for text in runs
            for pattern in (_SCRIPT_PATH, _SCRIPT_MODULE)
            for match in pattern.finditer(text)
        }
        if any(_installs_the_package(text) for text in runs):
            installing[job_id] = scripts
        else:
            lean[job_id] = scripts
            lean_runs[job_id] = runs
    return installing, lean, lean_runs


# --- AST: imports, including function-level and conditional ones -------------


@dataclass(frozen=True)
class ImportSite:
    function: str | None  # outermost def or class holding the import, None at module scope
    module: str  # dotted name as written; relative imports keep their leading dots
    names: tuple[str, ...]
    lineno: int


def import_sites(source: str) -> list[ImportSite]:
    """Every import statement anywhere in ``source``, plus dynamic-import calls."""

    sites: list[ImportSite] = []

    def visit(node: ast.AST, owner: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = owner
            if owner is None and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                child_owner = child.name
            if isinstance(child, ast.Import):
                for alias in child.names:
                    sites.append(ImportSite(child_owner, alias.name, (), child.lineno))
            elif isinstance(child, ast.ImportFrom):
                module = "." * child.level + (child.module or "")
                names = tuple(alias.name for alias in child.names)
                sites.append(ImportSite(child_owner, module, names, child.lineno))
            elif isinstance(child, ast.Call):
                func = child.func
                dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
                    isinstance(func, ast.Attribute) and func.attr == "import_module"
                )
                if dynamic:
                    sites.append(
                        ImportSite(child_owner, "<dynamic import>", (), child.lineno)
                    )
            visit(child, child_owner)

    visit(ast.parse(source), None)
    return sites


def resolve(site: ImportSite) -> tuple[set[str], list[str]]:
    """Split one import into (sibling script names, foreign module names).

    Standard-library imports resolve to neither. A bare name that is a file in
    ``scripts/`` is a sibling (the script directory is on ``sys.path`` when a
    script runs by path), as are ``scripts.X`` and one-dot relative imports.
    """

    siblings: set[str] = set()
    foreign: list[str] = []
    module = site.module
    if module == "<dynamic import>":
        foreign.append(module)
    elif module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".")
        if level != 1:
            foreign.append(module)
        elif rest:
            siblings.add(rest.split(".")[0])
        else:
            siblings.update(site.names)
    else:
        top, _, tail = module.partition(".")
        if top == "scripts":
            siblings.add("__init__")
            if tail:
                siblings.add(tail.split(".")[0])
            else:
                siblings.update(name for name in site.names if (SCRIPTS / f"{name}.py").is_file())
        elif top in STDLIB:
            pass
        elif (SCRIPTS / f"{top}.py").is_file():
            siblings.add(top)
        else:
            foreign.append(module)
    return siblings, foreign


# --- call graph: what a lean invocation can reach ----------------------------


def _mentions_semantic(test: ast.expr) -> bool:
    return any(
        (isinstance(node, ast.Name) and "semantic" in node.id.lower())
        or (isinstance(node, ast.Attribute) and "semantic" in node.attr.lower())
        for node in ast.walk(test)
    )


def _referenced_names(node: ast.AST, *, skip_semantic_branches: bool) -> set[str]:
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, name: ast.Name) -> None:
            names.add(name.id)

        def visit_If(self, branch: ast.If) -> None:
            if skip_semantic_branches and _mentions_semantic(branch.test):
                # The body runs only when a --semantic-eval-* option was passed.
                # The else branch runs when it was not, so it stays in.
                for statement in branch.orelse:
                    self.visit(statement)
                return
            self.generic_visit(branch)

    Collector().visit(node)
    return names


def functions_reachable_from_main(
    source: str, *, include_semantic_branches: bool
) -> set[str]:
    """Module-level functions reachable from ``main`` by name reference."""

    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "main" not in functions:
        return set(functions)  # nothing to prove from: treat everything as reachable
    pending = (
        _referenced_names(
            functions["main"], skip_semantic_branches=not include_semantic_branches
        )
        & functions.keys()
    )
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending |= (
            _referenced_names(functions[name], skip_semantic_branches=False)
            & functions.keys()
        ) - reached
    return reached


# --- the closure check -------------------------------------------------------


def closure_violations(entry_scripts: set[str]) -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    pending = sorted(entry_scripts)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = SCRIPTS / ("__init__.py" if name == "__init__" else f"{name}.py")
        if not path.is_file():
            violations.append(f"scripts/{name}.py is imported or run by a lean job but does not exist")
            continue
        source = path.read_text(encoding="utf-8")
        lean_reachable = functions_reachable_from_main(
            source, include_semantic_branches=False
        )
        for site in import_sites(source):
            siblings, foreign = resolve(site)
            pending.extend(sorted(siblings - seen))
            for module in foreign:
                allowed = INSTALLED_ONLY_IMPORTS.get((name, site.function or ""), frozenset())
                if module in allowed and name in entry_scripts and site.function not in lean_reachable:
                    continue
                where = f"function {site.function}" if site.function else "module scope"
                violations.append(
                    f"scripts/{name}.py:{site.lineno} imports {module!r} at {where}; "
                    "a lean job runs this script without the package installed"
                )
    return violations


# --- tests: the analyzers themselves -----------------------------------------


def test_job_classifier_separates_installing_jobs_from_lean_ones() -> None:
    text = """
jobs:
  full:
    steps:
      - run: |
          python -m pip install -e '.[dev]'
          python scripts/a.py
  lean:
    steps:
      - run: python -m pip install build==1.5.0
      - run: >-
          python scripts/b.py
          --flag
      - run: python -m scripts.c
  path-install:
    steps:
      - run: python -m pip install .
  wheel-install:
    steps:
      - run: pip install dist/*.whl
  published-install:
    steps:
      - run: pip install alice-memory==1.2.3
  none:
    steps:
      - uses: actions/checkout@v4
"""
    installing, lean, _runs = classify_jobs(text)
    assert set(installing) == {"full", "path-install", "wheel-install", "published-install"}
    assert installing["full"] == {"a"}
    assert lean == {"lean": {"b", "c"}, "none": set()}


def test_import_sites_follow_function_level_conditional_and_relative_imports() -> None:
    source = """
from __future__ import annotations
import json
import yaml
from alicebot_api.x import y

def f():
    from alicebot_api.vnext_evals import canonical

def g(flag):
    if flag:
        try:
            import numpy
        except ImportError:
            numpy = None

class C:
    def m(self):
        import importlib
        importlib.import_module("anything")

from . import sibling
from .other import thing
from .. import up
"""
    found = {(site.function, site.module) for site in import_sites(source)}
    assert (None, "yaml") in found
    assert (None, "alicebot_api.x") in found
    assert ("f", "alicebot_api.vnext_evals") in found
    assert ("g", "numpy") in found
    assert ("C", "<dynamic import>") in found
    assert (None, ".") in found and (None, ".other") in found and (None, "..") in found


def test_resolve_classifies_stdlib_siblings_and_foreign_modules() -> None:
    def classify(module: str, *names: str) -> tuple[set[str], list[str]]:
        return resolve(ImportSite(None, module, names, 1))

    assert classify("json") == (set(), [])
    assert classify("os.path") == (set(), [])
    assert classify("__future__") == (set(), [])
    assert classify("release_check") == ({"release_check"}, [])
    assert classify("scripts.release_check") == ({"__init__", "release_check"}, [])
    assert classify("scripts", "release_check", "not_a_module") == (
        {"__init__", "release_check"},
        [],
    )
    assert classify(".release_check") == ({"release_check"}, [])
    assert classify(".", "release_check") == ({"release_check"}, [])
    assert classify("yaml") == (set(), ["yaml"])
    assert classify("alicebot_api.vnext_evals") == (set(), ["alicebot_api.vnext_evals"])
    assert classify("..elsewhere") == (set(), ["..elsewhere"])
    assert classify("<dynamic import>") == (set(), ["<dynamic import>"])


def test_reachability_skips_only_the_semantic_branches_of_main() -> None:
    source = """
def deep(): pass
def semantic_only(): deep()
def always(): pass
def else_branch(): pass
def main(args):
    always()
    if args.semantic_eval_report is not None:
        semantic_only()
    else:
        else_branch()
"""
    lean = functions_reachable_from_main(source, include_semantic_branches=False)
    full = functions_reachable_from_main(source, include_semantic_branches=True)
    assert lean == {"always", "else_branch"}
    assert full == {"always", "else_branch", "semantic_only", "deep"}


# --- tests: the real workflow and scripts ------------------------------------


def _jobs() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, list[str]]]:
    return classify_jobs(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_has_both_installing_and_lean_jobs_that_run_scripts() -> None:
    installing, lean, _runs = _jobs()
    assert installing, "no job installs the project: the classifier or the workflow changed"
    assert lean, "no lean job: the classifier or the workflow changed"
    assert set().union(*lean.values()), "no lean job runs a script: nothing is being checked"


def test_lean_jobs_never_reference_the_package_in_their_own_commands() -> None:
    _installing, lean, runs = _jobs()
    assert lean
    for job_id, texts in runs.items():
        for text in texts:
            assert "alicebot_api" not in text, (job_id, text)
            assert not re.search(r"-m\s+alicebot", text), (job_id, text)


def test_lean_job_scripts_import_only_the_standard_library_and_sibling_scripts() -> None:
    """Closure of every script a lean job runs is stdlib or ``scripts/`` siblings.

    Mutation: put the import of ``CLAUDE_PLUGIN_ID`` from the package
    back in ``_marketplace_issues``, or add any import outside the standard library anywhere in
    a script a lean job runs. This test fails.
    """

    _installing, lean, _runs = _jobs()
    entry = set().union(*lean.values())
    assert closure_violations(entry) == []


def test_the_installed_only_import_is_unreachable_from_lean_invocations() -> None:
    """``_generator_release_contract`` keeps its import of the package, so prove it is dormant.

    The semantic eval validators need the generators in ``alicebot_api``. The
    workflow passes ``--semantic-eval-*`` only in the job that installs the
    project. Mutation: call ``validate_semantic_eval_report`` outside its
    ``if args.semantic_eval_report`` branch in ``main`` (or from
    ``validate_metadata``). This test fails.
    """

    _installing, lean, _runs = _jobs()
    entry = set().union(*lean.values())
    for (script, function), modules in INSTALLED_ONLY_IMPORTS.items():
        source = (SCRIPTS / f"{script}.py").read_text(encoding="utf-8")
        sites = {
            (site.function, site.module) for site in import_sites(source)
        }
        for module in modules:
            assert (function, module) in sites, f"stale allowlist entry: {script}.{function} imports {module}"
        full = functions_reachable_from_main(source, include_semantic_branches=True)
        lean_only = functions_reachable_from_main(source, include_semantic_branches=False)
        assert function in full, "the call graph no longer sees the semantic path: analysis is vacuous"
        assert function not in lean_only
        if script in entry:
            assert closure_violations({script}) == []


def test_the_marketplace_check_compares_against_the_script_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = ROOT / ".claude-plugin" / "marketplace.json"
    assert release_check._marketplace_issues(ROOT, market) == []
    monkeypatch.setattr(release_check, "CLAUDE_PLUGIN_ID", "other@elsewhere")
    issues = release_check._marketplace_issues(ROOT, market)
    assert len(issues) == 1
    assert "does not match CLAUDE_PLUGIN_ID" in issues[0]
    assert "alice-memory@alicememory" in issues[0]


# --- tests: execution without site-packages ----------------------------------


def _bare(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    """Run the current interpreter isolated (-I) and without site-packages (-S)."""

    return subprocess.run(
        [sys.executable, "-I", "-S", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="module")
def bare_interpreter() -> None:
    """Fail loudly if -I -S can still import the package: the tests below would prove nothing."""

    for module in ("alicebot_api", "yaml"):
        probe = _bare("-c", f"import {module}")
        assert probe.returncode != 0 and "ModuleNotFoundError" in probe.stderr, (
            f"python -I -S can import {module}; the bare-interpreter tests are vacuous"
        )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if head.returncode != 0:
        pytest.skip("not a git checkout: release_check reads Git state")


def _assert_no_import_failure(done: subprocess.CompletedProcess[str]) -> None:
    combined = done.stdout + done.stderr
    assert "ModuleNotFoundError" not in combined, combined
    assert "ImportError" not in combined, combined
    assert "Traceback" not in combined, combined


def test_release_check_runs_bare_through_validate_metadata_with_the_marketplace_file(
    bare_interpreter: None,
) -> None:
    """The path that failed in the v0.19.1 draft readback, on a bare interpreter.

    Mutation: put the ``alicebot_api`` import back in ``_marketplace_issues``.
    This test fails with ModuleNotFoundError.
    """

    assert (ROOT / ".claude-plugin" / "marketplace.json").is_file()
    done = _bare("scripts/release_check.py")
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Release check: PASS" in done.stdout


def _write_synthetic_release_assets(dist_dir: Path) -> tuple[str, list[Path]]:
    from tests.unit.test_release_check import _write_distribution_pair

    metadata, metadata_issues = release_check.validate_metadata(ROOT)
    assert metadata_issues == []
    description, description_issues = release_check.validate_package_description_source(ROOT)
    assert description_issues == [] and description is not None
    _write_distribution_pair(
        dist_dir,
        version=metadata.version,
        wheel_description=description,
        sdist_description=description,
    )
    artifacts, issues = release_check.validate_distributions(
        dist_dir, version=metadata.version, expected_description=description
    )
    assert issues == []
    release_check.write_checksums(dist_dir, artifacts)
    return metadata.version, artifacts


def test_release_check_verifies_release_assets_bare(
    bare_interpreter: None, tmp_path: Path
) -> None:
    """``--dist-dir D --verify-release-assets``, the exact readback invocation, bare."""

    _version, _artifacts = _write_synthetic_release_assets(tmp_path / "draft-readback")
    done = _bare(
        "scripts/release_check.py",
        "--dist-dir",
        str(tmp_path / "draft-readback"),
        "--verify-release-assets",
    )
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Release check: PASS" in done.stdout


def test_release_check_still_fails_closed_bare_on_a_tampered_release_asset(
    bare_interpreter: None, tmp_path: Path
) -> None:
    dist_dir = tmp_path / "draft-readback"
    _version, artifacts = _write_synthetic_release_assets(dist_dir)
    (dist_dir / "extra.txt").write_text("not part of the release", encoding="utf-8")
    done = _bare(
        "scripts/release_check.py",
        "--dist-dir",
        str(dist_dir),
        "--verify-release-assets",
    )
    _assert_no_import_failure(done)
    assert done.returncode == 1
    assert "Release check: FAIL" in done.stdout
    assert "release asset set" in done.stdout


def test_every_lean_job_script_starts_bare(bare_interpreter: None) -> None:
    _installing, lean, _runs = _jobs()
    names = sorted(set().union(*lean.values()))
    assert names
    for name in names:
        done = _bare(f"scripts/{name}.py", "--help")
        _assert_no_import_failure(done)
        assert done.returncode == 0, (name, done.stdout + done.stderr)


def test_the_release_body_helpers_and_sdist_normalizer_run_bare(
    bare_interpreter: None, tmp_path: Path
) -> None:
    dist_dir = tmp_path / "verified"
    version, artifacts = _write_synthetic_release_assets(dist_dir)
    commit = "a" * 40

    rendered = tmp_path / "body.md"
    done = _bare(
        "scripts/render_release_body.py",
        "--repository",
        "owner/repo",
        "--tag",
        f"v{version}",
        "--commit-sha",
        commit,
        "--checksums",
        str(dist_dir / "SHA256SUMS"),
        "--output",
        str(rendered),
    )
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert rendered.read_text(encoding="utf-8") == render_release_body.render_release_body(
        repository="owner/repo",
        tag=f"v{version}",
        commit_sha=commit,
        checksum_manifest=dist_dir / "SHA256SUMS",
    )

    release_json = tmp_path / "release.json"
    release_json.write_text(json.dumps({"body": rendered.read_text(encoding="utf-8")}), encoding="utf-8")
    decoded = tmp_path / "decoded.md"
    done = _bare(
        "scripts/decode_github_release_body.py",
        "--input",
        str(release_json),
        "--output",
        str(decoded),
    )
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert decoded.read_bytes() == rendered.read_bytes()
    assert decode_github_release_body.decode_release_body(release_json.read_bytes()) == decoded.read_bytes()

    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    bare_copy = tmp_path / "bare" / sdist.name
    inproc_copy = tmp_path / "inproc" / sdist.name
    for copy in (bare_copy, inproc_copy):
        copy.parent.mkdir()
        copy.write_bytes(sdist.read_bytes())
    done = _bare(
        "scripts/normalize_sdist.py",
        "--source-date-epoch",
        "1700000000",
        str(bare_copy),
    )
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    normalize_sdist.normalize_gzip_timestamp(inproc_copy, source_date_epoch=1700000000)
    assert bare_copy.read_bytes() == inproc_copy.read_bytes()
