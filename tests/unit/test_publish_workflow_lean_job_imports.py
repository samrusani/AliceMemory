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
  every job that does not install the project, collects the scripts it runs
  (by path, with ``./`` and workspace prefixes, and by ``-m scripts.X``), and
  proves by AST that each script's whole import closure is the standard
  library or a sibling ``scripts/`` module, following function-level and
  conditional imports. A script run by path has its own directory on
  ``sys.path`` and not the repository root, so only a bare sibling name
  resolves for it; ``from scripts import X`` and ``from . import X`` do not.
  Tripwires fail a lean step that mentions ``scripts/`` or ``.py`` and yields no
  collected script, and a lean step that runs inline Python (``python -c``, a
  heredoc, stdin) unless it is allowlisted with a reason;
* a proof, by call graph from ``main()``, that the one installed-only import
  that is left cannot be reached by an invocation a lean job makes. A branch
  of ``main()`` is skipped only when its test is a positive presence test of a
  ``--semantic-eval-*`` option (``X is not None`` or plain truthiness); every
  other test, negated or compound, counts as reachable;
* execution tests that run the scripts under ``python -I -S`` (isolated, no
  site-packages) on every path the lean jobs take: the draft readback, the
  release body helpers, the sdist normalizer, the rebuild comparison of the
  resume job, and the finalize, resume and recovery invocations with
  ``--tag`` and PyPI answered by an offline stub.

Mutations that must fail this file, each alone:

* put the import of ``CLAUDE_PLUGIN_ID`` from the package back inside
  ``_marketplace_issues`` in ``scripts/release_check.py``: the structural guard
  and the bare execution tests fail;
* add ``import yaml`` (or any import outside the standard library) at the top or inside a
  function of any script a lean job runs, or add a lean job step that runs
  ``python scripts/run_phase5_ops_evidence.py`` (also as
  ``./scripts/run_phase5_ops_evidence.py`` or
  ``"$GITHUB_WORKSPACE/scripts/run_phase5_ops_evidence.py"``): the structural
  guard fails;
* add ``from scripts import normalize_sdist`` (or ``from . import normalize_sdist``)
  to a script a lean job runs by path, at module scope or inside a function: the
  structural guard fails, because that import cannot work when the script is run
  by path. A bare ``import normalize_sdist`` is the form that works;
* add a lean job step with ``python -c "import yaml"``, a python heredoc, or a
  run line that mentions ``scripts/`` or ``.py`` in a form the collector does
  not read: the tripwires fail;
* call ``validate_semantic_eval_report`` from ``validate_metadata``, or call it
  under a negated or compound test in ``main``, so a lean invocation reaches
  the installed-only import: the reachability test fails;
* pass a ``--semantic-eval-*`` option in a lean job: the premise of that proof
  fails;
* raise ``NameError`` on the ``--compare-dist-dir`` path, or on the PyPI verify
  path, of ``scripts/release_check.py``: the matching bare execution test fails,
  although the structural guard sees no import;
* change the value of ``CLAUDE_PLUGIN_ID`` in ``scripts/release_check.py``:
  ``test_the_release_check_plugin_id_equals_the_installed_constant`` in
  ``tests/unit/test_claude_code_plugin.py`` fails. That test lives there, not
  here, because it imports the package's ``host_install`` module and the real-host CI
  path filter lists every test that does.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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

PATH_RUN = "path"  # python scripts/X.py: the script directory is sys.path[0]
MODULE_RUN = "module"  # python -m scripts.X: the working directory is sys.path[0]

_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b(?P<rest>[^\n;&|]*)")
# A runner step reaches a script as scripts/X.py, ./scripts/X.py or through the
# workspace variable. A preceding word character, dot, slash or hyphen means a
# different directory (docs/scripts/X.py, ../scripts/X.py), which is not matched.
_WORKSPACE_PREFIX = (
    r"(?:\./"
    r"|[\"']?\$\{?(?:GITHUB_WORKSPACE|PWD)\}?[\"']?/"
    r"|\$\{\{\s*github\.workspace\s*\}\}/"
    r"|\$\(pwd\)/)"
)
_SCRIPT_PATH = re.compile(
    rf"(?<![\w./-])(?:{_WORKSPACE_PREFIX})?scripts/(?P<name>[A-Za-z_]\w*)\.py\b"
)
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


def run_modes(run_texts: Iterable[str]) -> dict[str, set[str]]:
    """Script name to how the given steps start it: by path, by ``-m scripts.X``, or both."""

    modes: dict[str, set[str]] = {}
    for text in run_texts:
        for mode, pattern in ((PATH_RUN, _SCRIPT_PATH), (MODULE_RUN, _SCRIPT_MODULE)):
            for match in pattern.finditer(text):
                modes.setdefault(match.group("name"), set()).add(mode)
    return modes


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
        scripts = set(run_modes(runs))
        if any(_installs_the_package(text) for text in runs):
            installing[job_id] = scripts
        else:
            lean[job_id] = scripts
            lean_runs[job_id] = runs
    return installing, lean, lean_runs


# --- tripwires: lean steps the script collector cannot read ------------------

# An inline Python step in a lean job runs code that is not a file under
# scripts/, so the import closure never sees it. Key: (job id, the step line that
# starts Python, whitespace collapsed). Value: why it is safe on a bare
# interpreter. An entry whose line no longer appears fails as stale.
INLINE_PYTHON_ALLOWLIST: dict[tuple[str, str], str] = {}

_PYTHON_WORD = re.compile(r"(?:^|(?<=[\s;&|(`\"'=]))(?:[\w./$-]*/)?python[\d.]*(?=\s|$|[)`\"'])")
_COMMAND_END = frozenset({";", ";;", "&&", "||", "|", "|&", "&", ")", "`"})
_REDIRECT = re.compile(r"[0-9]*[<>&]+")


def _python_arguments_are_inline(arguments: list[str], *, stdin_script: bool) -> bool:
    """True when the Python arguments name code, not a file or a module.

    ``-c CODE`` and ``-`` carry code. ``-m MODULE`` and a script path do not.
    With none of those, Python reads its program from standard input, which is
    code when it is a heredoc or a pipe (``stdin_script``).
    """

    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "-":
            return True
        if token.startswith("--"):
            return False  # --version, --help
        if token.startswith("-"):
            for position, flag in enumerate(token[1:], start=1):
                if flag == "c":
                    return True
                if flag == "m":
                    return False
                if flag in "WX":  # takes a value: attached, or the next token
                    if position == len(token) - 1:
                        index += 1
                    break
            index += 1
            continue
        return False  # the first positional argument is a script path
    return stdin_script


def inline_python_lines(run_text: str) -> list[str]:
    """Lines of a run step that start Python on code that is not a repository file.

    That is ``python -c``, ``python -`` and a heredoc or piped program. A line
    that cannot be tokenized is reported too, because it cannot be shown safe.
    """

    found: list[str] = []
    for line in run_text.replace("\\\n", " ").splitlines():
        for match in _PYTHON_WORD.finditer(line):
            lexer = shlex.shlex(line[match.end() :], posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            arguments: list[str] = []
            stdin_fed = line[: match.start()].rstrip().endswith("|")
            skip_target = False
            try:
                for token in lexer:
                    if token in _COMMAND_END:
                        break
                    if skip_target:  # the file or heredoc word after a redirect
                        skip_target = False
                    elif _REDIRECT.fullmatch(token):
                        skip_target = True
                        stdin_fed = stdin_fed or token.startswith("<")
                    else:
                        arguments.append(token)
            except ValueError:
                found.append(" ".join(line.split()))
                break
            if _python_arguments_are_inline(arguments, stdin_script=stdin_fed):
                found.append(" ".join(line.split()))
                break
    return found


def _mentions_a_script(run_text: str) -> bool:
    return "scripts/" in run_text or re.search(r"\.py\b", run_text) is not None


def lean_step_violations(
    lean_runs: Mapping[str, list[str]],
    allowlist: Mapping[tuple[str, str], str],
) -> list[str]:
    """Lean steps the closure check cannot see into, plus allowlist bookkeeping."""

    violations: list[str] = []
    used: set[tuple[str, str]] = set()
    for job_id, texts in lean_runs.items():
        for text in texts:
            if _mentions_a_script(text) and not run_modes([text]):
                violations.append(
                    f"{job_id}: a step mentions scripts/ or .py but yields no script the guard reads, "
                    f"so its imports are not checked: {text.strip().splitlines()[0]!r}"
                )
            for line in inline_python_lines(text):
                key = (job_id, line)
                if key in allowlist:
                    used.add(key)
                else:
                    violations.append(
                        f"{job_id}: inline Python in a lean job is not covered by the import check "
                        f"(allowlist it with a reason, or move it into a script): {line!r}"
                    )
    for key, reason in allowlist.items():
        if not reason.strip():
            violations.append(f"allowlist entry {key} has no reason")
        elif key not in used:
            violations.append(f"stale allowlist entry, no such inline Python in a lean job: {key}")
    return violations


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


def resolve(
    site: ImportSite, *, mode: str = PATH_RUN, scripts_dir: Path = SCRIPTS
) -> tuple[set[str], list[str]]:
    """Split one import into (sibling script names, foreign module names).

    Standard-library imports resolve to neither. What counts as a sibling
    depends on how the script is started, because that decides ``sys.path``:

    * ``python scripts/X.py`` (``PATH_RUN``, how every lean job starts a script)
      puts the ``scripts/`` directory first and not the repository root, and
      the script has no parent package. A bare name that is a file in
      ``scripts/`` is a sibling. ``scripts``, ``scripts.X`` and relative
      imports all fail at runtime, so they are foreign.
    * ``python -m scripts.X`` (``MODULE_RUN``) puts the working directory first
      and runs the script inside the ``scripts`` package. ``scripts.X`` and
      one-dot relative imports are siblings. A bare sibling name is foreign.
    """

    siblings: set[str] = set()
    foreign: list[str] = []
    module = site.module
    if module == "<dynamic import>":
        foreign.append(module)
    elif module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".")
        if mode == PATH_RUN or level != 1:
            foreign.append(module)
        elif rest:
            siblings.add(rest.split(".")[0])
        else:
            siblings.update(site.names)
    else:
        top, _, tail = module.partition(".")
        if top == "scripts":
            if mode == PATH_RUN:
                foreign.append(module)
            else:
                siblings.add("__init__")
                if tail:
                    siblings.add(tail.split(".")[0])
                else:
                    siblings.update(
                        name for name in site.names if (scripts_dir / f"{name}.py").is_file()
                    )
        elif top in STDLIB:
            pass
        elif mode == PATH_RUN and (scripts_dir / f"{top}.py").is_file():
            siblings.add(top)
        else:
            foreign.append(module)
    return siblings, foreign


# --- call graph: what a lean invocation can reach ----------------------------


def _names_a_semantic_option(node: ast.expr) -> bool:
    """A bare name or attribute that is a --semantic-eval-* option or a value derived from one.

    ``args.semantic_eval_report`` and the local ``semantic_report_path`` both count.
    """

    identifier = (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else None
    )
    return identifier is not None and "semantic" in identifier.lower()


def _is_positive_semantic_presence_test(test: ast.expr) -> bool:
    """True only for ``X is not None`` and plain ``X``, where X is a semantic option.

    Those are the tests whose body runs only when the option was passed. A
    negated test (``X is None``, ``not X``), a compound one (``X is None or Y``)
    or any other shape has a body that runs on an invocation that passes no
    semantic option, so it is not skipped.
    """

    if _names_a_semantic_option(test):
        return True
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
        and _names_a_semantic_option(test.left)
    )


def _referenced_names(node: ast.AST, *, skip_semantic_branches: bool) -> set[str]:
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, name: ast.Name) -> None:
            names.add(name.id)

        def visit_If(self, branch: ast.If) -> None:
            if skip_semantic_branches and _is_positive_semantic_presence_test(branch.test):
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


def closure_violations(
    entry_scripts: set[str],
    *,
    modes: Mapping[str, set[str]] | None = None,
    scripts_dir: Path = SCRIPTS,
) -> list[str]:
    """Imports in the closure of ``entry_scripts`` that a lean job could not satisfy.

    ``modes`` says how each entry script is started (``run_modes``); an entry
    not listed is started by path. A sibling is loaded the way the script that
    imports it was started, so it is checked under the same mode.
    """

    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    pending = [
        (name, mode)
        for name in sorted(entry_scripts)
        for mode in sorted((modes or {}).get(name) or {PATH_RUN})
    ]
    while pending:
        name, mode = pending.pop()
        if (name, mode) in seen:
            continue
        seen.add((name, mode))
        path = scripts_dir / ("__init__.py" if name == "__init__" else f"{name}.py")
        if not path.is_file():
            violations.append(f"scripts/{name}.py is imported or run by a lean job but does not exist")
            continue
        source = path.read_text(encoding="utf-8")
        lean_reachable = functions_reachable_from_main(
            source, include_semantic_branches=False
        )
        for site in import_sites(source):
            siblings, foreign = resolve(site, mode=mode, scripts_dir=scripts_dir)
            pending.extend((sibling, mode) for sibling in sorted(siblings))
            for module in foreign:
                allowed = INSTALLED_ONLY_IMPORTS.get((name, site.function or ""), frozenset())
                if module in allowed and name in entry_scripts and site.function not in lean_reachable:
                    continue
                where = f"function {site.function}" if site.function else "module scope"
                run = "by path" if mode == PATH_RUN else "as -m scripts.X"
                violations.append(
                    f"scripts/{name}.py:{site.lineno} imports {module!r} at {where}; "
                    f"a lean job runs this script {run} without the package installed"
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


@pytest.mark.parametrize(
    "run_text",
    (
        "python scripts/x.py --flag",
        "python3 ./scripts/x.py --flag",
        'python "$GITHUB_WORKSPACE/scripts/x.py"',
        'python "$GITHUB_WORKSPACE"/scripts/x.py',
        "python ${GITHUB_WORKSPACE}/scripts/x.py",
        "python ${{ github.workspace }}/scripts/x.py",
        "python $PWD/scripts/x.py",
        "python $(pwd)/scripts/x.py",
        "python 'scripts/x.py'",
        "python -I -S scripts/x.py",
        "echo go && python ./scripts/x.py",
        "python \\\n  ./scripts/x.py",
    ),
)
def test_script_discovery_reads_every_way_a_step_starts_a_script(run_text: str) -> None:
    assert run_modes([run_text]) == {"x": {PATH_RUN}}


@pytest.mark.parametrize(
    "run_text",
    (
        "python docs/scripts/x.py",
        "python ../scripts/x.py",
        "python ./other/scripts/x.py",
        "python my-scripts/x.py",
        "python /opt/scripts/x.py",
        "python $HOME/scripts/x.py",
        "python scripts/nested/x.py",
        "cat scripts/x.json",
    ),
)
def test_script_discovery_ignores_other_directories(run_text: str) -> None:
    assert run_modes([run_text]) == {}


def test_script_discovery_tells_path_runs_from_module_runs() -> None:
    texts = ["python scripts/a.py", "python -m scripts.b", "python scripts/c.py && python -m scripts.c"]
    assert run_modes(texts) == {"a": {PATH_RUN}, "b": {MODULE_RUN}, "c": {PATH_RUN, MODULE_RUN}}


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
    """A script run by path has ``scripts/`` on ``sys.path``, not the repository root.

    Mutation: make ``resolve`` treat ``scripts``, ``scripts.X`` or a relative import as a
    sibling for ``PATH_RUN`` again. These assertions and the closure tests below fail.
    """

    def classify(module: str, *names: str, mode: str = PATH_RUN) -> tuple[set[str], list[str]]:
        return resolve(ImportSite(None, module, names, 1), mode=mode)

    # Standard library: neither, in both modes.
    for mode in (PATH_RUN, MODULE_RUN):
        assert classify("json", mode=mode) == (set(), [])
        assert classify("os.path", mode=mode) == (set(), [])
        assert classify("__future__", mode=mode) == (set(), [])
        assert classify("yaml", mode=mode) == (set(), ["yaml"])
        assert classify("alicebot_api.vnext_evals", mode=mode) == (set(), ["alicebot_api.vnext_evals"])
        assert classify("..elsewhere", mode=mode) == (set(), ["..elsewhere"])
        assert classify("<dynamic import>", mode=mode) == (set(), ["<dynamic import>"])

    # python scripts/X.py: only a bare sibling name resolves.
    assert classify("release_check") == ({"release_check"}, [])
    assert classify("scripts.release_check") == (set(), ["scripts.release_check"])
    assert classify("scripts", "release_check") == (set(), ["scripts"])
    assert classify("scripts", "release_check", "not_a_module") == (set(), ["scripts"])
    assert classify(".release_check") == (set(), [".release_check"])
    assert classify(".", "release_check") == (set(), ["."])

    # python -m scripts.X: the package forms resolve, a bare sibling name does not.
    assert classify("scripts.release_check", mode=MODULE_RUN) == ({"__init__", "release_check"}, [])
    assert classify("scripts", "release_check", "not_a_module", mode=MODULE_RUN) == (
        {"__init__", "release_check"},
        [],
    )
    assert classify(".release_check", mode=MODULE_RUN) == ({"release_check"}, [])
    assert classify(".", "release_check", mode=MODULE_RUN) == ({"release_check"}, [])
    assert classify("release_check", mode=MODULE_RUN) == (set(), ["release_check"])


def _scripts_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
    for name, source in files.items():
        (scripts_dir / f"{name}.py").write_text(source, encoding="utf-8")
    return scripts_dir


@pytest.mark.parametrize(
    "import_line",
    (
        "from scripts import helper",
        "from scripts.helper import value",
        "import scripts.helper",
        "from . import helper",
        "from .helper import value",
    ),
)
def test_a_path_run_script_cannot_import_a_sibling_through_the_package(
    tmp_path: Path, import_line: str
) -> None:
    """The v0.19.1 shape of failure, one level up: ``python scripts/X.py`` cannot see ``scripts.helper``.

    Mutation: let ``resolve`` accept these forms for ``PATH_RUN``. This test fails.
    """

    scripts_dir = _scripts_dir(tmp_path, {"entry": f"{import_line}\n", "helper": "value = 1\n"})
    violations = closure_violations({"entry"}, scripts_dir=scripts_dir)
    assert len(violations) == 1, violations
    assert "scripts/entry.py:1" in violations[0]
    assert "by path" in violations[0]


def test_a_path_run_script_may_import_a_bare_sibling_and_its_imports_are_checked(tmp_path: Path) -> None:
    scripts_dir = _scripts_dir(
        tmp_path,
        {"entry": "import helper\n", "helper": "import json\nimport yaml\n"},
    )
    violations = closure_violations({"entry"}, scripts_dir=scripts_dir)
    assert len(violations) == 1, violations
    assert "scripts/helper.py:2" in violations[0] and "'yaml'" in violations[0]

    scripts_dir = _scripts_dir(tmp_path / "clean", {"entry": "import helper\n", "helper": "import json\n"})
    assert closure_violations({"entry"}, scripts_dir=scripts_dir) == []


def test_a_sibling_is_checked_under_the_mode_of_the_script_that_imports_it(tmp_path: Path) -> None:
    """A path-run entry's helper is loaded as a top-level module, so its own ``scripts.X`` import fails too."""

    scripts_dir = _scripts_dir(
        tmp_path,
        {
            "entry": "import helper\n",
            "helper": "from scripts import leaf\n",
            "leaf": "value = 1\n",
        },
    )
    violations = closure_violations({"entry"}, scripts_dir=scripts_dir)
    assert len(violations) == 1, violations
    assert "scripts/helper.py:1" in violations[0]


def test_a_module_run_script_may_import_through_the_package_but_not_by_bare_name(tmp_path: Path) -> None:
    scripts_dir = _scripts_dir(
        tmp_path,
        {"entry": "from scripts import helper\nfrom . import leaf\n", "helper": "value = 1\n", "leaf": "x = 1\n"},
    )
    modes = {"entry": {MODULE_RUN}}
    assert closure_violations({"entry"}, modes=modes, scripts_dir=scripts_dir) == []

    scripts_dir = _scripts_dir(tmp_path / "bare", {"entry": "import helper\n", "helper": "value = 1\n"})
    violations = closure_violations({"entry"}, modes=modes, scripts_dir=scripts_dir)
    assert len(violations) == 1, violations
    assert "as -m scripts.X" in violations[0]

    # The sibling is loaded as scripts.helper, so its own bare sibling import fails too.
    scripts_dir = _scripts_dir(
        tmp_path / "inherited",
        {"entry": "from scripts import helper\n", "helper": "import leaf\n", "leaf": "x = 1\n"},
    )
    violations = closure_violations({"entry"}, modes=modes, scripts_dir=scripts_dir)
    assert len(violations) == 1, violations
    assert "scripts/helper.py:1" in violations[0] and "as -m scripts.X" in violations[0]


def test_a_script_started_both_ways_is_held_to_both(tmp_path: Path) -> None:
    scripts_dir = _scripts_dir(tmp_path, {"entry": "import helper\n", "helper": "value = 1\n"})
    both = {"entry": {PATH_RUN, MODULE_RUN}}
    violations = closure_violations({"entry"}, modes=both, scripts_dir=scripts_dir)
    assert len(violations) == 1 and "as -m scripts.X" in violations[0], violations


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


_REACHABILITY_TEMPLATE = """
def target(): pass
def other(): pass
def main(args, flag):
    other()
    if {test}:
        target()
"""


@pytest.mark.parametrize(
    "test",
    (
        "args.semantic_eval_report is not None",
        "args.write_semantic_eval_attestation is not None",
        "semantic_report_path is not None",
        "args.semantic_eval_report",
        "semantic_report_path",
    ),
)
def test_a_positive_presence_test_of_a_semantic_option_skips_its_body(test: str) -> None:
    source = _REACHABILITY_TEMPLATE.format(test=test)
    assert functions_reachable_from_main(source, include_semantic_branches=False) == {"other"}
    assert functions_reachable_from_main(source, include_semantic_branches=True) == {"other", "target"}


@pytest.mark.parametrize(
    "test",
    (
        "args.semantic_eval_report is None",
        "not args.semantic_eval_report",
        "args.semantic_eval_report is not None or flag",
        "flag or args.semantic_eval_report",
        "args.semantic_eval_report is None or args.semantic_eval_attestation is None",
        "args.semantic_eval_report is not None and flag",
        "args.semantic_eval_report is not False",
        "args.semantic_eval_report != None",
        "args.semantic_eval_report is not None is not False",
        "args.semantic_eval_report.exists()",
        "bool(args.semantic_eval_report) is False",
        "flag",
        "args.expected_sha is not None",
    ),
)
def test_any_other_test_keeps_its_body_reachable(test: str) -> None:
    """Negated and compound tests run on a lean invocation, which passes no semantic option.

    Mutation: skip every branch whose test mentions "semantic" again. These cases fail.
    """

    source = _REACHABILITY_TEMPLATE.format(test=test)
    assert functions_reachable_from_main(source, include_semantic_branches=False) == {"other", "target"}


def test_an_elif_chain_keeps_the_branches_that_run_without_the_option() -> None:
    source = """
def a(): pass
def b(): pass
def c(): pass
def main(args):
    if args.semantic_eval_report is not None:
        a()
    elif not args.semantic_eval_attestation:
        b()
    else:
        c()
"""
    assert functions_reachable_from_main(source, include_semantic_branches=False) == {"b", "c"}


def test_every_semantic_option_defaults_to_none() -> None:
    """``X is not None`` and plain truthiness are presence tests only while the default is None."""

    options = [
        action
        for action in release_check.build_parser()._actions
        if any("semantic" in flag for flag in action.option_strings)
    ]
    assert {flag for action in options for flag in action.option_strings} == {
        "--semantic-eval-report",
        "--write-semantic-eval-attestation",
        "--semantic-eval-attestation",
    }
    for action in options:
        assert action.default is None, action.option_strings


# --- tests: the tripwires ----------------------------------------------------


_INLINE_PYTHON = (
    'python -c "print(1)"',
    "python3 -c 'import tomllib; print(1)'",
    "python -Ic 'import yaml'",
    "python -I -c 'x'",
    "python -W error -c 'x'",
    "python - <<'EOF'\nimport yaml\nEOF",
    "python3 <<EOF\nimport yaml\nEOF",
    "python <<-PY\nimport yaml\nPY",
    "cat program.txt | python -",
    "cat program.txt | python",
    "python < program.txt",
    'echo "value=$(python -c \'print(1)\')" >> "$GITHUB_OUTPUT"',
    'python -c "\nimport yaml\n"',
    "true && python3.12 -c 'x'",
    "/usr/bin/python3 -c 'x'",
)

_NOT_INLINE_PYTHON = (
    "python scripts/x.py --flag",
    "python -m scripts.c",
    "python -m pip install build==1.5.0",
    "python -m build --outdir dist",
    "python3 --version",
    "python -I -S scripts/x.py",
    "python -X dev scripts/x.py",
    "python -W error::DeprecationWarning scripts/x.py",
    "python scripts/x.py <<EOF\nanswer\nEOF",
    "python scripts/x.py 2>&1",
    "pip install python-dateutil",
    "echo $pythonLocation",
    "cmp a b",
)


@pytest.mark.parametrize("run_text", _INLINE_PYTHON)
def test_inline_python_is_detected(run_text: str) -> None:
    assert inline_python_lines(run_text), run_text


@pytest.mark.parametrize("run_text", _NOT_INLINE_PYTHON)
def test_python_that_runs_a_file_or_a_module_is_not_inline(run_text: str) -> None:
    assert inline_python_lines(run_text) == [], run_text


def test_the_tripwires_fail_inline_python_and_unread_script_mentions_in_a_lean_job() -> None:
    """Mutation: drop either tripwire from ``lean_step_violations``. The matching case fails."""

    lean_runs = {"lean": ["python -m pip install build==1.5.0", "python scripts/x.py --flag"]}
    assert lean_step_violations(lean_runs, {}) == []

    # Inline Python: not a file under scripts/, so the closure check never sees it.
    for step in ('python -c "import yaml"', "python - <<'EOF'\nimport yaml\nEOF"):
        violations = lean_step_violations({"lean": [step]}, {})
        assert len(violations) == 1 and "inline Python" in violations[0], (step, violations)

    # A step that names a script in a form the collector cannot read.
    for step in (
        "cd scripts && python x.py",
        "python tools/x.py",
        "python scripts/nested/x.py",
        "python \"$GITHUB_WORKSPACE/../scripts/x.py\"",
        "for f in scripts/*.py; do python \"$f\"; done",
    ):
        violations = lean_step_violations({"lean": [step]}, {})
        assert len(violations) == 1 and "yields no script the guard reads" in violations[0], (step, violations)


def test_the_inline_python_allowlist_needs_a_reason_and_goes_stale() -> None:
    step = "python -c 'print(1)'"
    line = "python -c 'print(1)'"
    lean_runs = {"lean": [step]}
    assert lean_step_violations(lean_runs, {("lean", line): "prints a constant, imports nothing"}) == []
    # The reason is required.
    blank = lean_step_violations(lean_runs, {("lean", line): "  "})
    assert any("has no reason" in violation for violation in blank), blank
    # The entry is bound to its job and its line.
    other_job = lean_step_violations(lean_runs, {("other", line): "reason"})
    assert any("is not covered by the import check" in violation for violation in other_job), other_job
    assert any("stale allowlist entry" in violation for violation in other_job), other_job
    # An entry whose line is gone fails as stale.
    gone = lean_step_violations({"lean": ["python scripts/x.py"]}, {("lean", line): "reason"})
    assert gone == [f"stale allowlist entry, no such inline Python in a lean job: ('lean', {line!r})"]


# --- tests: the real workflow and scripts ------------------------------------


def _jobs() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, list[str]]]:
    return classify_jobs(WORKFLOW.read_text(encoding="utf-8"))


def _lean_modes() -> dict[str, set[str]]:
    """How the lean jobs start each script they run."""

    _installing, _lean, runs = _jobs()
    return run_modes(text for texts in runs.values() for text in texts)


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
    assert closure_violations(entry, modes=_lean_modes()) == []


def test_lean_job_steps_hide_no_script_and_run_no_unlisted_inline_python() -> None:
    """Every script a lean step mentions is one the closure check read, and no step runs inline Python.

    Mutation: add ``python -c "import yaml"`` or a python heredoc to a lean job, or
    start a script in a form the collector does not read (``cd scripts && python x.py``).
    """

    _installing, _lean, runs = _jobs()
    assert lean_step_violations(runs, INLINE_PYTHON_ALLOWLIST) == []


def test_lean_jobs_pass_no_semantic_eval_option() -> None:
    """The reachability proof below assumes a lean invocation passes no ``--semantic-eval-*`` option.

    Mutation: add ``--semantic-eval-report`` or ``--semantic-eval-attestation`` to a step of a
    job that does not install the project. This test fails.
    """

    _installing, _lean, runs = _jobs()
    for job_id, texts in runs.items():
        for text in texts:
            assert "semantic-eval" not in text, (job_id, text)


def test_the_installed_only_import_is_unreachable_from_lean_invocations() -> None:
    """``_generator_release_contract`` keeps its import of the package, so prove it is dormant.

    The semantic eval validators need the generators in ``alicebot_api``. The
    workflow passes ``--semantic-eval-*`` only in the job that installs the
    project. Mutation: call ``validate_semantic_eval_report`` outside its
    ``if args.semantic_eval_report is not None`` branch in ``main``, under a negated or
    compound test, or from ``validate_metadata``. This test fails.
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
            assert closure_violations({script}, modes=_lean_modes()) == []


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


# --- tests: the rebuild comparison and the finalize, resume and recover runs --
#
# The resume job runs ``release_check.py --tag T --dist-dir verified
# --compare-dist-dir deterministic-rebuild --verify-release-assets
# --verify-pypi-artifact-subset``. The finalize job and the recovery mode run
# ``--tag T --dist-dir verified --verify-release-assets --verify-pypi-artifacts``.
# All three run on the bare interpreter. ``--tag`` reads the git tag and the
# finalized release docs, so those runs use a small synthetic repository with
# an annotated tag (the real checkout's HEAD is not a tagged release). PyPI is
# answered by a stdlib-only wrapper, so nothing here touches the network.

_OFFLINE_PYPI_WRAPPER = r'''"""Run a script under -I -S with PyPI answered from a recorded file.

usage: wrapper.py RECORDED REQUEST_LOG SCRIPT [ARGS...]

Standard library only. urllib.request.urlopen is replaced before the script is
loaded, so the script's own ``from urllib.request import urlopen`` gets the
stub. Every URL asked for is appended to REQUEST_LOG, and a URL with no
recorded answer raises, so nothing reaches the network.
"""
import io
import json
import os
import runpy
import sys
import urllib.error
import urllib.request

recorded_path, request_log, script, *script_args = sys.argv[1:]
with open(recorded_path, encoding="utf-8") as handle:
    recorded = json.load(handle)


class Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def offline_urlopen(request, timeout=None):
    url = getattr(request, "full_url", request)
    with open(request_log, "a", encoding="utf-8") as log:
        log.write(url + "\n")
    answer = recorded.get(url)
    if answer is None:
        raise urllib.error.URLError("offline stub: no recorded answer for " + url)
    if answer["status"] != 200:
        raise urllib.error.HTTPError(url, answer["status"], "recorded", {}, None)
    return Reply(json.dumps(answer["body"]).encode("utf-8"))


urllib.request.urlopen = offline_urlopen
sys.argv = [script, *script_args]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))  # what ``python SCRIPT`` does
runpy.run_path(script, run_name="__main__")
'''

# Everything validate_metadata and the finalized-docs check read from the repository root.
_RELEASE_CHECK_INPUTS = (
    "pyproject.toml",
    "docs/pypi-description.md",
    "apps/web/package.json",
    "packaging/mcpb/manifest.json",
    "plugins/alice-memory/.claude-plugin/plugin.json",
    "plugins/alice-memory/.mcp.json",
    "plugins/alice-memory/hooks/hooks.json",
    "apps/api/src/alicebot_api/main.py",
    "apps/api/src/alicebot_api/__init__.py",
)
_OPTIONAL_RELEASE_CHECK_INPUTS = (".claude-plugin/marketplace.json",)


def _git_env() -> dict[str, str]:
    """Git settings for the synthetic repository: no inherited GIT_* state, no user config."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="Release Test",
        GIT_AUTHOR_EMAIL="release-test",
        GIT_COMMITTER_NAME="Release Test",
        GIT_COMMITTER_EMAIL="release-test",
    )
    return env


def _synthetic_release_repo(base: Path, version: str) -> Path:
    """A git repository holding a finalized, pre-publication release, tagged ``v<version>`` at HEAD."""

    repo = base / "release-repo"
    for relative in _RELEASE_CHECK_INPUTS:
        assert (ROOT / relative).is_file(), f"release_check reads {relative} and it is gone"
    for relative in (*_RELEASE_CHECK_INPUTS, *_OPTIONAL_RELEASE_CHECK_INPUTS):
        if (ROOT / relative).is_file():
            (repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, repo / relative)
    headings = [f"v{version}"]
    marketplace = repo / ".claude-plugin" / "marketplace.json"
    if marketplace.is_file():
        ref = json.loads(marketplace.read_text(encoding="utf-8"))["plugins"][0]["source"]["ref"]
        headings.append(ref)  # the marketplace check wants a dated heading for its pinned ref
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n"
        + "".join(f"## {heading} \u2014 2026-01-01\n\n- Synthetic.\n\n" for heading in dict.fromkeys(headings)),
        encoding="utf-8",
    )
    state = {
        "schema_version": release_check.RELEASE_DOCUMENT_STATE_SCHEMA_VERSION,
        "version": version,
        "publication_status": "pending",
        "checksums_status": "pending",
    }
    notes = repo / "docs" / "release" / f"v{version}-release-notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        f"# Alice v{version} Release Notes\n"
        f"<!-- alice-release-state: {json.dumps(state, separators=(',', ':'))} -->\n\nSynthetic.\n",
        encoding="utf-8",
    )
    for command in (
        ("init", "-q", "-b", "main"),
        ("add", "."),
        ("commit", "-q", "-m", "Synthetic release commit"),
        ("tag", "-a", f"v{version}", "-m", f"v{version}"),
    ):
        subprocess.run(["git", *command], cwd=repo, env=_git_env(), check=True, capture_output=True)
    return repo


def _pypi_url(version: str) -> str:
    return f"https://pypi.org/pypi/alice-memory/{version}/json"


def _record_pypi(
    path: Path,
    version: str,
    files: Iterable[Path],
    *,
    wrong_digest_for: str | None = None,
    status: int = 200,
) -> Path:
    """Write the answer PyPI would give for ``files``, optionally with one wrong digest."""

    urls = [
        {
            "filename": file.name,
            "digests": {
                "sha256": "0" * 64
                if file.name == wrong_digest_for
                else sha256(file.read_bytes()).hexdigest()
            },
        }
        for file in files
    ]
    body = {"urls": urls}
    path.write_text(
        json.dumps({_pypi_url(version): {"status": status, "body": body}}), encoding="utf-8"
    )
    return path


def _bare_with_offline_pypi(
    tmp_path: Path, recorded: Path, *args: str, cwd: Path
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """``python -I -S release_check.py ARGS`` with PyPI served from ``recorded``; also the URLs asked."""

    wrapper = tmp_path / "offline_pypi_wrapper.py"
    wrapper.write_text(_OFFLINE_PYPI_WRAPPER, encoding="utf-8")
    request_log = tmp_path / "pypi-requests.log"
    request_log.unlink(missing_ok=True)
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(wrapper),
            str(recorded),
            str(request_log),
            str(SCRIPTS / "release_check.py"),
            *args,
        ],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    requests = request_log.read_text(encoding="utf-8").splitlines() if request_log.exists() else []
    return done, requests


def _copy_distributions(artifacts: Iterable[Path], target: Path) -> Path:
    """What the resume job's deterministic rebuild leaves: the wheel and the sdist, no manifest."""

    target.mkdir()
    for artifact in artifacts:
        shutil.copyfile(artifact, target / artifact.name)
    return target


def test_release_check_compares_a_deterministic_rebuild_bare(
    bare_interpreter: None, tmp_path: Path
) -> None:
    """``--dist-dir D --compare-dist-dir R``, the resume job's rebuild comparison, bare and offline.

    Mutation: raise ``NameError`` in ``compare_distribution_artifacts`` or on the
    ``--compare-dist-dir`` branch of ``main``. The first run fails.
    """

    verified = tmp_path / "verified"
    _version, artifacts = _write_synthetic_release_assets(verified)
    rebuild = _copy_distributions(artifacts, tmp_path / "deterministic-rebuild")
    args = (
        "scripts/release_check.py",
        "--dist-dir",
        str(verified),
        "--compare-dist-dir",
        str(rebuild),
        "--verify-release-assets",
    )

    done = _bare(*args)
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Release check: PASS" in done.stdout

    # Same content, different gzip header time: the comparison fails closed on the bytes.
    sdist = next(rebuild.glob("*.tar.gz"))
    sdist.write_bytes(gzip.compress(gzip.decompress(sdist.read_bytes()), mtime=1))
    done = _bare(*args)
    _assert_no_import_failure(done)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "Release check: FAIL" in done.stdout
    assert "deterministic rebuild does not match canonical artifact" in done.stdout


def test_release_check_runs_the_finalize_and_recover_invocation_bare(
    bare_interpreter: None, tmp_path: Path
) -> None:
    """``--tag T --dist-dir D --verify-release-assets --verify-pypi-artifacts`` with PyPI stubbed.

    Mutation: raise ``NameError`` in ``verify_pypi_artifacts`` or in ``validate_git_state``'s
    tag branch. This test fails, although the import check sees nothing.
    """

    verified = tmp_path / "verified"
    version, artifacts = _write_synthetic_release_assets(verified)
    repo = _synthetic_release_repo(tmp_path, version)
    args = (
        "--root",
        str(repo),
        "--tag",
        f"v{version}",
        "--dist-dir",
        str(verified),
        "--verify-release-assets",
        "--verify-pypi-artifacts",
    )

    recorded = _record_pypi(tmp_path / "pypi-published.json", version, artifacts)
    done, requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Release check: PASS" in done.stdout
    assert requests == [_pypi_url(version)], "the PyPI check did not run against the stub"

    wheel = next(path for path in artifacts if path.name.endswith(".whl"))
    recorded = _record_pypi(
        tmp_path / "pypi-other-bytes.json", version, artifacts, wrong_digest_for=wheel.name
    )
    done, requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "PyPI sha256 does not match verified artifact" in done.stdout
    assert requests == [_pypi_url(version)]

    recorded = _record_pypi(tmp_path / "pypi-partial.json", version, [wheel])
    done, _requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "PyPI file set is not an exact permitted set of verified artifacts" in done.stdout


def test_release_check_runs_the_resume_invocation_bare(
    bare_interpreter: None, tmp_path: Path
) -> None:
    """The resume job's last call: ``--tag``, ``--compare-dist-dir`` and ``--verify-pypi-artifact-subset``.

    Mutation: raise ``NameError`` on the subset branch of ``verify_pypi_artifacts`` or on
    the ``--compare-dist-dir`` branch of ``main``. This test fails.
    """

    verified = tmp_path / "verified"
    version, artifacts = _write_synthetic_release_assets(verified)
    rebuild = _copy_distributions(artifacts, tmp_path / "deterministic-rebuild")
    repo = _synthetic_release_repo(tmp_path, version)
    args = (
        "--root",
        str(repo),
        "--tag",
        f"v{version}",
        "--dist-dir",
        str(verified),
        "--compare-dist-dir",
        str(rebuild),
        "--verify-release-assets",
        "--verify-pypi-artifact-subset",
    )
    wheel = next(path for path in artifacts if path.name.endswith(".whl"))

    # A partial upload: PyPI holds the wheel and not the sdist.
    recorded = _record_pypi(tmp_path / "pypi-partial.json", version, [wheel])
    done, requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Release check: PASS" in done.stdout
    assert requests == [_pypi_url(version)], "the PyPI check did not run against the stub"

    # Nothing is missing, so this is not a resume: finalization recovery is the right mode.
    recorded = _record_pypi(tmp_path / "pypi-complete.json", version, artifacts)
    done, _requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "PyPI resume requires a proper partial file set" in done.stdout

    # The published wheel carries other bytes than the verified one.
    recorded = _record_pypi(
        tmp_path / "pypi-other-bytes.json", version, [wheel], wrong_digest_for=wheel.name
    )
    done, _requests = _bare_with_offline_pypi(tmp_path, recorded, *args, cwd=repo)
    _assert_no_import_failure(done)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "PyPI sha256 does not match verified artifact" in done.stdout
