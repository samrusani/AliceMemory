"""No workflow job holds a write scope and also installs a mutable package.

A job that installs ``@latest`` (or an unpinned or upgraded package) runs code
its upstream can change at any time. A write scope on the same job puts that
scope in reach of that code. The fix is a separate job for the write step. These
tests read the workflow files. They do not call GitHub.

The detector reads the specs a job names itself. A local install such as
``pip install -e '.[dev]'`` resolves dependency ranges below it and is not
flagged, so a job that installs only that is judged on its own permissions.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# (workflow file, job) -> why it is still allowed. Remove an entry when its job is split.
KNOWN_RESIDUAL = {
    (
        "archive-maintenance.yml",
        "archive-maintenance",
    ): "nightly job: issues: write, pip --upgrade pip and the dev dependency ranges; split the alert step out",
}

_NPM = re.compile(r"\bnpm\s+(?:install|i|add)\s+([^\n]*)")
_NPX = re.compile(r"\b(?:npx|uvx|pipx\s+run)\s+(\S+)")
_PIP = re.compile(r"""\bpip(?:3)?["']?\s+install\s+([^\n]*)""")
_EXACT_NPM = re.compile(r"@\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def mutable_installs(script: str) -> list[str]:
    """Package specs in a shell script that an upstream publish can change."""

    found: list[str] = []
    for match in _NPM.finditer(script):
        for token in match.group(1).split():
            if token.startswith("-"):
                continue
            token = token.strip("'\"")
            if token in {".", "./"} or token.startswith(("./", "/")):
                continue
            if not _EXACT_NPM.search(token):
                found.append(f"npm:{token}")
    for match in _NPX.finditer(script):
        if not _EXACT_NPM.search(match.group(1).strip("'\"")) and "==" not in match.group(1):
            found.append(f"run:{match.group(1)}")
    for match in _PIP.finditer(script):
        args = match.group(1).split()
        if "--upgrade" in args or "-U" in args:
            found.append("pip:--upgrade")
        for token in args:
            if token.startswith("-"):
                continue
            token = token.strip("'\"")
            if token.startswith((".", "/", "dist/")) or token.endswith((".whl", ".tar.gz")):
                continue
            if token in {"pip", "setuptools", "wheel"} and ("--upgrade" in args or "-U" in args):
                continue
            if "==" not in token:
                found.append(f"pip:{token}")
    return found


class _Loader(yaml.SafeLoader):
    """Safe YAML that keeps the workflow key ``on`` as a string."""


_Loader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in patterns if tag != "tag:yaml.org,2002:bool"]
    for key, patterns in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _offenders() -> dict[tuple[str, str], tuple[list[str], list[str]]]:
    offenders: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for path in WORKFLOWS:
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
        inherited = workflow.get("permissions")
        for name, job in workflow["jobs"].items():
            permissions = job.get("permissions", inherited)
            # no permissions key anywhere means the repository default token, which can write
            writes = (
                ["<repository default>"]
                if permissions is None
                else sorted(scope for scope, level in permissions.items() if level == "write")
            )
            scripts = "\n".join(
                step.get("run", "") for step in job.get("steps", []) if isinstance(step, dict)
            )
            mutable = mutable_installs(scripts)
            if writes and mutable:
                offenders[(path.name, name)] = (writes, mutable)
    return offenders


def test_the_detector_sees_the_specs_the_workflows_use() -> None:
    """The detector flags mutable specs and accepts exact pins and local installs.

    Mutation: make the detector accept ``@latest``, a bare pip name, or
    ``--upgrade``; or make it flag an exact pin or ``-e '.[dev]'``. This test
    fails.
    """

    assert mutable_installs("npm install -g @anthropic-ai/claude-code@latest") == [
        "npm:@anthropic-ai/claude-code@latest"
    ]
    assert mutable_installs("npm install -g opencode-ai") == ["npm:opencode-ai"]
    assert mutable_installs("npm install -g @openai/codex@0.158.0") == []
    assert mutable_installs("npm install -g opencode-ai@1.18.32") == []
    assert mutable_installs('"$RUNNER_TEMP/hermes-venv/bin/pip" install hermes-agent') == [
        "pip:hermes-agent"
    ]
    assert mutable_installs("pip install --disable-pip-version-check 'hermes-agent==0.19.0'") == []
    assert "pip:--upgrade" in mutable_installs("python -m pip install --upgrade pip")
    assert mutable_installs("python -m pip install -e '.[dev]'") == []
    assert mutable_installs("python -m pip install build==1.5.0") == []
    assert mutable_installs("python -m pip install dist/*.whl") == []
    assert mutable_installs('python -m pip install "bandit>=1.8,<2.0"') == ["pip:bandit>=1.8,<2.0"]
    assert mutable_installs("npx some-tool") == ["run:some-tool"]
    assert mutable_installs("uvx some-tool@latest") == ["run:some-tool@latest"]


def test_no_write_scope_job_installs_a_mutable_package() -> None:
    """Only the listed residual may hold a write scope and install a mutable package.

    Mutation: add ``issues: write`` (or ``contents: write``, ``id-token: write``)
    to the canary job, or to any job that runs ``pip install <name>`` or
    ``npm install <name>@latest``; add a new write-scope job that installs a
    bare name. This test fails.
    """

    offenders = _offenders()
    unexpected = {key: value for key, value in offenders.items() if key not in KNOWN_RESIDUAL}
    assert not unexpected, unexpected


def test_the_known_residual_list_has_no_stale_entries() -> None:
    """A fixed job must leave the list, so the list never hides a new regression.

    Mutation: leave ``archive-maintenance`` listed after its alert step is
    split out; list a job that does not exist. This test fails.
    """

    offenders = _offenders()
    stale = sorted(key for key in KNOWN_RESIDUAL if key not in offenders)
    assert not stale, stale
