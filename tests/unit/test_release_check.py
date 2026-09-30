from __future__ import annotations

from copy import deepcopy
import io
import json
from hashlib import sha256
import math
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

import alicebot_api.vnext_evals as vnext_evals
from alicebot_api.vnext_evals import (
    VNEXT_ACCEPTANCE_TARGETS,
    VNEXT_EVAL_DATABASE_URL_ENV,
    VNEXT_EVAL_SUITE_ORDER,
    generate_correction_suppression_corpus,
    generate_decision_recovery_corpus,
    generate_entity_resolution_corpus,
    generate_graph_hop_corpus,
    generate_provenance_explanation_corpus,
    generate_vnext_benchmark_corpus,
    run_vnext_evals,
)
import scripts.release_check as release_check


def _seed_metadata_tree(tmp_path: Path, *, python_version: str, web_version: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                'name = "alice-memory"',
                f'version = "{python_version}"',
                'readme = "docs/pypi-description.md"',
                "",
            )
        ),
        encoding="utf-8",
    )
    description = tmp_path / "docs" / "pypi-description.md"
    description.parent.mkdir(parents=True)
    description.write_text("# Alice Memory\n\nEvergreen package description.\n", encoding="utf-8")
    web_dir = tmp_path / "apps" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text(
        f'{{"name":"@alicebot/web","private":true,"version":"{web_version}"}}\n',
        encoding="utf-8",
    )
    package_dir = tmp_path / "apps" / "api" / "src" / "alicebot_api"
    package_dir.mkdir(parents=True)
    (package_dir / "main.py").write_text(
        'app = FastAPI(title="AliceBot API", version=__version__)\n',
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        "from importlib.metadata import version as _distribution_version\n"
        '__version__ = _distribution_version("alice-memory")\n',
        encoding="utf-8",
    )
    manifest_dir = tmp_path / "packaging" / "mcpb"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"version": python_version}) + "\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "plugins" / "alice-memory"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / "hooks").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "alice-memory", "version": python_version}) + "\n",
        encoding="utf-8",
    )
    pin = f"alice-memory=={python_version}"
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"alice": {"command": "uvx", "args": ["--from", pin]}}}) + "\n",
        encoding="utf-8",
    )
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": "uvx",
                                    "args": ["--from", pin, "alice-memory-session-start"],
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_distribution_pair(
    dist_dir: Path,
    *,
    version: str,
    wheel_description: str,
    sdist_description: str,
) -> None:
    dist_dir.mkdir(parents=True)
    wheel = dist_dir / f"alice_memory-{version}-py3-none-any.whl"
    wheel_metadata = (
        "Metadata-Version: 2.4\n"
        "Name: alice-memory\n"
        f"Version: {version}\n"
        "Description-Content-Type: text/markdown\n\n"
        f"{wheel_description}"
    )
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            f"alice_memory-{version}.dist-info/METADATA",
            wheel_metadata,
        )
        archive.writestr("alicebot_api/_resources/alembic.ini", "[alembic]\n")
        archive.writestr("alicebot_api/_resources/alembic/env.py", "# env\n")
        archive.writestr(
            "alicebot_api/_resources/alembic/versions/20260714_0090_example.py",
            "revision = 'example'\n",
        )
        archive.writestr(
            "alicebot_api/_resources/eval/public_eval_suites.json",
            "{}\n",
        )

    sdist = dist_dir / f"alice_memory-{version}.tar.gz"
    root = f"alice_memory-{version}"
    sdist_metadata = (
        "Metadata-Version: 2.4\n"
        "Name: alice-memory\n"
        f"Version: {version}\n"
        "Description-Content-Type: text/markdown\n\n"
        f"{sdist_description}"
    )

    def add_text(archive: tarfile.TarFile, name: str, content: str) -> None:
        payload = content.encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with tarfile.open(sdist, mode="w:gz") as archive:
        add_text(archive, f"{root}/PKG-INFO", sdist_metadata)
        add_text(
            archive,
            f"{root}/pyproject.toml",
            f'[project]\nname = "alice-memory"\nversion = "{version}"\n',
        )
        add_text(archive, f"{root}/apps/api/alembic.ini", "[alembic]\n")
        add_text(archive, f"{root}/apps/api/alembic/env.py", "# env\n")
        add_text(archive, f"{root}/eval/fixtures/public_eval_suites.json", "{}\n")
        add_text(archive, f"{root}/setup.py", "# setup\n")


def test_release_metadata_uses_pyproject_as_canonical_version(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")

    metadata, issues = release_check.validate_metadata(tmp_path)

    assert issues == []
    assert metadata.version == "1.2.3"
    assert metadata.tag == "v1.2.3"


def test_release_metadata_rejects_mcpb_version_drift(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    manifest = tmp_path / "packaging" / "mcpb" / "manifest.json"
    manifest.write_text('{"version": "9.9.9"}\n', encoding="utf-8")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("manifest.json version does not match pyproject.toml" in issue for issue in issues)
    assert any("9.9.9" in issue for issue in issues)


def test_release_metadata_reports_a_missing_mcpb_manifest(tmp_path: Path) -> None:
    """A missing manifest is an issue string. Raising fails this test."""

    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    manifest = tmp_path / "packaging" / "mcpb" / "manifest.json"
    manifest.unlink()

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert issues == ["packaging/mcpb/manifest.json is missing or unreadable"]


def test_release_metadata_reports_an_unreadable_mcpb_manifest(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    manifest = tmp_path / "packaging" / "mcpb" / "manifest.json"
    manifest.unlink()
    manifest.mkdir()

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert issues == ["packaging/mcpb/manifest.json is missing or unreadable"]


def test_mcpb_manifest_version_matches_pyproject() -> None:
    """The committed MCPB manifest matches pyproject. Drift fails this test."""

    root = Path(__file__).resolve().parents[2]
    pyproject = release_check._read_toml(root / "pyproject.toml")
    project = pyproject.get("project")
    assert isinstance(project, dict)
    version = project.get("version")
    manifest = json.loads((root / "packaging" / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version


def test_release_metadata_rejects_web_version_drift(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.2")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("package.json version does not match pyproject.toml" in issue for issue in issues)


def test_release_metadata_rejects_missing_configured_readme(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    (tmp_path / "docs" / "pypi-description.md").unlink()

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("pypi-description.md" in issue for issue in issues)


def test_release_metadata_rejects_prerelease_version(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3rc1", web_version="1.2.3rc1")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("stable SemVer" in issue for issue in issues)


@pytest.mark.parametrize(
    "description",
    (
        "Alice v1.2.3 package description.\n",
        "Alice is the latest published release.\n",
        "Alice is in release gating.\n",
        "Alice is an unpublished candidate.\n",
    ),
)
def test_release_metadata_rejects_version_state_in_package_description(
    tmp_path: Path,
    description: str,
) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    (tmp_path / release_check.PACKAGE_DESCRIPTION_RELATIVE_PATH).write_text(
        description,
        encoding="utf-8",
    )

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("package description" in issue or "pypi-description" in issue for issue in issues)


def test_distribution_metadata_uses_the_evergreen_package_description(
    tmp_path: Path,
) -> None:
    expected = "# Alice Memory\n\nEvergreen package description.\n"
    dist_dir = tmp_path / "dist"
    _write_distribution_pair(
        dist_dir,
        version="1.2.3",
        wheel_description=expected,
        sdist_description=expected,
    )

    _artifacts, issues = release_check.validate_distributions(
        dist_dir,
        version="1.2.3",
        expected_description=expected,
    )

    assert issues == []


def test_distribution_metadata_rejects_stale_readme_descriptions(
    tmp_path: Path,
) -> None:
    expected = "# Alice Memory\n\nEvergreen package description.\n"
    dist_dir = tmp_path / "dist"
    _write_distribution_pair(
        dist_dir,
        version="1.2.3",
        wheel_description="The candidate is in release gating.\n",
        sdist_description="The previous release is the latest published release.\n",
    )

    _artifacts, issues = release_check.validate_distributions(
        dist_dir,
        version="1.2.3",
        expected_description=expected,
    )

    assert "wheel METADATA long description does not match the evergreen package description" in issues
    assert "sdist PKG-INFO long description does not match the evergreen package description" in issues


def test_release_metadata_accepts_fastapi_subclass_constructor(tmp_path: Path) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    main_path = tmp_path / "apps" / "api" / "src" / "alicebot_api" / "main.py"
    main_path.write_text(
        "class AliceFastAPI:\n    pass\n\n"
        "app: AliceFastAPI = AliceFastAPI(\n"
        '    title="AliceBot API",\n'
        "    version=__version__,\n"
        ")\n",
        encoding="utf-8",
    )

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert issues == []


@pytest.mark.parametrize(
    "package_source",
    (
        "from importlib.metadata import version as installed_version\n"
        "__version__ = installed_version('alice-memory')\n",
        "from importlib import metadata as installed_metadata\n"
        '__version__ = installed_metadata.version("alice-memory")\n',
        "import importlib.metadata\n"
        '__version__ = importlib.metadata.version("alice-memory")\n',
    ),
)
def test_release_metadata_accepts_semantic_distribution_version_forms(
    tmp_path: Path,
    package_source: str,
) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    package_init = tmp_path / "apps" / "api" / "src" / "alicebot_api" / "__init__.py"
    package_init.write_text(package_source, encoding="utf-8")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert issues == []


@pytest.mark.parametrize(
    "package_source",
    (
        "# _distribution_version('alice-memory')\n__version__ = '1.2.3'\n",
        "from importlib.metadata import version\n"
        "def hidden():\n    return version('alice-memory')\n"
        "__version__ = '1.2.3'\n",
        "from importlib.metadata import version\n"
        "__version__ = version('different-package')\n",
        "from importlib.metadata import version\n"
        "__version__ = version('alice-memory')\n"
        "__version__ = 'overridden'\n",
    ),
)
def test_release_metadata_rejects_distribution_version_false_positives(
    tmp_path: Path,
    package_source: str,
) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    package_init = tmp_path / "apps" / "api" / "src" / "alicebot_api" / "__init__.py"
    package_init.write_text(package_source, encoding="utf-8")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("installed distribution metadata" in issue for issue in issues)


@pytest.mark.parametrize(
    "app_source",
    (
        'app = FastAPI(title="AliceBot API", version="1.2.3")\n',
        'app = FastAPI(title="AliceBot API")\n',
        'def build():\n    return FastAPI(version=__version__)\napp = build()\n',
    ),
)
def test_release_metadata_rejects_app_without_distribution_version(
    tmp_path: Path, app_source: str
) -> None:
    _seed_metadata_tree(tmp_path, python_version="1.2.3", web_version="1.2.3")
    main_path = tmp_path / "apps" / "api" / "src" / "alicebot_api" / "main.py"
    main_path.write_text(app_source, encoding="utf-8")

    _metadata, issues = release_check.validate_metadata(tmp_path)

    assert any("FastAPI application version" in issue for issue in issues)


def test_checksum_manifest_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "alice_memory-1.2.3.tar.gz"
    second = tmp_path / "alice_memory-1.2.3-py3-none-any.whl"
    first.write_bytes(b"sdist")
    second.write_bytes(b"wheel")

    manifest = release_check.write_checksums(tmp_path, [second, first])

    assert manifest.read_text(encoding="utf-8").splitlines() == [
        "ba59926159d2aa256eb8739b8da7e2b574b960e1202c6d624cbe981cef996c91  alice_memory-1.2.3-py3-none-any.whl",
        "714772a9f82b2aeb4fa5f7092d00fe4ac4c9cdeb6800840b6ed39ea64c4d785a  alice_memory-1.2.3.tar.gz",
    ]


def test_release_git_identity_resolves_annotated_tag_to_exact_main_commit(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Release Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "release@example.invalid"], check=True)
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "candidate"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "tag", "-a", "v1.2.3", "-m", "v1.2.3"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    issues = release_check.validate_git_state(
        root_dir=repo,
        tag="v1.2.3",
        expected_sha=head,
        require_main_head=True,
        require_clean=True,
    )

    assert issues == []


def test_release_git_identity_rejects_lightweight_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Release Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "release@example.invalid"],
        check=True,
    )
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "candidate"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "tag", "v1.2.3"], check=True)

    issues = release_check.validate_git_state(
        root_dir=repo,
        tag="v1.2.3",
        expected_sha=None,
        require_main_head=False,
        require_clean=True,
    )

    assert any("must be an annotated tag" in issue for issue in issues)


def test_finalized_release_docs_require_dated_changelog_and_final_title(tmp_path: Path) -> None:
    release_dir = tmp_path / "docs" / "release"
    release_dir.mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n## v1.2.3 — 2026-07-11\n\n- Ready.\n",
        encoding="utf-8",
    )
    (release_dir / "v1.2.3-release-notes.md").write_text(
        "# Alice v1.2.3 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"1.2.3","publication_status":"pending",'
        '"checksums_status":"pending"} -->\n\nReady for publication.\n',
        encoding="utf-8",
    )

    assert release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    ) == []

    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
    (release_dir / "v1.2.3-release-notes.md").write_text(
        "# Alice v1.2.3 Release Candidate Notes\n\nNot published yet.\n",
        encoding="utf-8",
    )
    issues = release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    )
    assert any("finalized dated heading" in issue for issue in issues)
    assert any("finalized title" in issue for issue in issues)
    assert any("alice-release-state" in issue for issue in issues)


def _seed_finalized_docs(tmp_path: Path, notes_verify_section: str) -> Path:
    release_dir = tmp_path / "docs" / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n## v1.2.3 — 2026-07-11\n\n- Ready.\n",
        encoding="utf-8",
    )
    notes = release_dir / "v1.2.3-release-notes.md"
    notes.write_text(
        "# Alice v1.2.3 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"1.2.3","publication_status":"pending",'
        '"checksums_status":"pending"} -->\n\nReady.\n\n'
        "## Verifying this release\n\n" + notes_verify_section + "\n",
        encoding="utf-8",
    )
    return notes


def test_finalized_release_docs_reject_structured_published_state(tmp_path: Path) -> None:
    notes = _seed_finalized_docs(
        tmp_path,
        "Publication prose is not parsed for keywords.",
    )
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            '"publication_status":"pending"',
            '"publication_status":"published"',
        ),
        encoding="utf-8",
    )

    issues = release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    )

    assert any("publication_status" in issue for issue in issues), issues


def test_finalized_release_docs_reject_structured_recorded_checksums_state(
    tmp_path: Path,
) -> None:
    notes = _seed_finalized_docs(
        tmp_path,
        "Checksum prose is not parsed for keyword variants.",
    )
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            '"checksums_status":"pending"',
            '"checksums_status":"recorded"',
        ),
        encoding="utf-8",
    )

    issues = release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    )

    assert any("checksums_status" in issue for issue in issues), issues


def test_finalized_release_docs_ignore_quoted_or_synonymous_publication_prose(
    tmp_path: Path,
) -> None:
    _seed_finalized_docs(
        tmp_path,
        "The migration guide quotes ‘are published to PyPI’, while another "
        "example says artifacts were uploaded to PyPI. The structured state "
        "above is authoritative and remains pending.",
    )

    assert release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    ) == []


@pytest.mark.parametrize(
    "case",
    ("duplicate_fenced", "fenced_bypass", "misplaced", "malformed"),
)
def test_finalized_release_docs_reject_noncanonical_state_declarations(
    tmp_path: Path,
    case: str,
) -> None:
    notes = _seed_finalized_docs(tmp_path, "Verify.")
    content = notes.read_text(encoding="utf-8")
    valid_line = content.splitlines()[1]
    if case == "duplicate_fenced":
        content += (
            "\n```markdown\n"
            '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
            '"version":"1.2.3","publication_status":"published",'
            '"checksums_status":"recorded"} -->\n'
            "```\n"
        )
    elif case == "fenced_bypass":
        published_line = valid_line.replace(
            '"publication_status":"pending","checksums_status":"pending"',
            '"publication_status":"published","checksums_status":"recorded"',
        )
        content = (
            "# Alice v1.2.3 Release Notes\n"
            "```markdown\n"
            f"{valid_line}\n"
            "```\n"
            f"{published_line}\n"
            "Ready.\n"
        )
    elif case == "misplaced":
        content = content.replace(valid_line + "\n", "\n" + valid_line + "\n", 1)
    else:
        content = content.replace(valid_line, "<!-- alice-release-state: not-json -->", 1)
    notes.write_text(content, encoding="utf-8")

    issues = release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    )

    assert any("alice-release-state" in issue for issue in issues), issues


def test_finalized_release_docs_require_empty_unreleased_section(tmp_path: Path) -> None:
    release_dir = tmp_path / "docs" / "release"
    release_dir.mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Still pending.\n\n"
        "## v1.2.3 — 2026-07-11\n\n- Ready.\n",
        encoding="utf-8",
    )
    (release_dir / "v1.2.3-release-notes.md").write_text(
        "# Alice v1.2.3 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"1.2.3","publication_status":"pending",'
        '"checksums_status":"pending"} -->\n\nReady for publication.\n',
        encoding="utf-8",
    )

    issues = release_check.validate_release_document_state(
        tmp_path, version="1.2.3", require_finalized=True
    )

    assert any("Unreleased section must be empty" in issue for issue in issues)


def _embedding_signature() -> dict[str, object]:
    provider = "openai_compatible"
    model = "release-embedding-v1"
    return {
        "schema_version": "alice_embedding_signature_identity_v1",
        "signature_version": 2,
        "provider": provider,
        "provider_fingerprint": sha256(provider.encode("utf-8")).hexdigest(),
        "model": model,
        "model_fingerprint": sha256(model.encode("utf-8")).hexdigest(),
        "endpoint_fingerprint": "0123456789abcdef",
    }


@pytest.fixture(scope="module")
def canonical_semantic_eval_report() -> dict[str, object]:
    """A realistic 78-case report assembled by the production generator.

    The store-backed suites run through their real SQLite-compatible service
    paths in isolated transactions. The protected-job-only boundaries are
    injected: deterministic threshold-passing retrieval with three honest
    paraphrase misses, the Postgres backend label, and the non-secret embedding
    identity/seeding record.
    """
    corpus = generate_vnext_benchmark_corpus()
    expected_by_query = {
        str(query["query"]): str(query["expected_memory_key"])
        for query in corpus["queries"]
    }
    missed_queries = {
        str(query["query"])
        for query in corpus["queries"]
        if query["query_key"] in {"paraphrase-004", "paraphrase-011", "paraphrase-016"}
    }

    def threshold_passing_retrieval(query: str, *, limit: int) -> dict[str, object]:
        del limit
        return {
            "ranked_memory_keys": (
                ["vnext-eval/retrieval/distractor-001"]
                if query in missed_queries
                else [expected_by_query[query]]
            ),
            "vector_stage": "enabled",
            "vector_candidate_count": 20,
        }

    signature = _embedding_signature()
    original_retrieval_runner = vnext_evals.run_retrieval_quality_eval

    def release_retrieval_runner(*args: object, **kwargs: object) -> dict[str, object]:
        suite = original_retrieval_runner(*args, **kwargs)
        suite["metrics"]["seeding"] = {
            "seeded_memory_count": 216,
            "embedded_memory_count": 216,
            "embedding_signature": deepcopy(signature),
            "embedding_note": "embedded via openai_compatible/release-embedding-v1",
        }
        return suite

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")
        monkeypatch.setattr(vnext_evals, "_eval_backend_label", lambda *_args, **_kwargs: "postgres")
        monkeypatch.setattr(vnext_evals, "run_retrieval_quality_eval", release_retrieval_runner)
        report = run_vnext_evals(
            suite="all",
            retrieval_fn=threshold_passing_retrieval,
            release_gate=True,
        )

    assert report["status"] == "pass"
    assert report["summary"]["case_count"] == 78
    assert report["summary"]["passed_case_count"] == 75
    assert report["summary"]["failed_case_count"] == 3
    assert release_check.validate_semantic_eval_report(report) == []
    return report


def _refresh_semantic_report_digest(report: dict[str, object]) -> None:
    report["report_digest"] = release_check._semantic_eval_report_digest(report)


def _reconcile_retrieval_mrr(report: dict[str, object]) -> None:
    retrieval = report["suites"][0]
    cases = retrieval["cases"]
    retrieval["metrics"]["mrr"] = sum(
        case["metrics"]["reciprocal_rank"] for case in cases
    ) / len(cases)
    for subset_key in ("lexical_overlap", "paraphrase"):
        subset_cases = [case for case in cases if case["subset"] == subset_key]
        retrieval["metrics"]["subsets"][subset_key]["mrr"] = sum(
            case["metrics"]["reciprocal_rank"] for case in subset_cases
        ) / len(subset_cases)


def _write_semantic_eval_artifact_pair(
    *,
    tmp_path: Path,
    report: dict[str, object],
    source_sha: str = "a" * 40,
) -> tuple[Path, Path]:
    report_path = tmp_path / "semantic-eval-report.json"
    attestation_path = tmp_path / "semantic-eval-attestation.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_check.write_semantic_eval_attestation(
        report_path=report_path,
        attestation_path=attestation_path,
        source_sha=source_sha,
    )
    return report_path, attestation_path


def _json_nodes(
    value: object,
    *,
    path: tuple[str | int, ...] = (),
) -> list[tuple[tuple[str | int, ...], object]]:
    nodes = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            nodes.extend(_json_nodes(child, path=(*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            nodes.extend(_json_nodes(child, path=(*path, index)))
    return nodes


def _replace_json_node(
    value: object,
    *,
    path: tuple[str | int, ...],
    replacement: object,
) -> object:
    if not path:
        return replacement
    updated = deepcopy(value)
    parent = updated
    for segment in path[:-1]:
        assert isinstance(parent, (dict, list))
        parent = parent[segment]  # type: ignore[index]
    assert isinstance(parent, (dict, list))
    parent[path[-1]] = replacement  # type: ignore[index]
    return updated


def test_release_verifier_canonical_contract_tracks_generator_definitions(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    generated_corpora = {
        "retrieval_quality": generate_vnext_benchmark_corpus(),
        "correction_suppression": generate_correction_suppression_corpus(),
        "decision_recovery": generate_decision_recovery_corpus(),
        "provenance_explanation": generate_provenance_explanation_corpus(),
        "entity_resolution": generate_entity_resolution_corpus(),
        "graph_hop_retrieval": generate_graph_hop_corpus(),
    }
    generated_case_keys = {
        "retrieval_quality": tuple(
            str(row["query_key"])
            for row in generated_corpora["retrieval_quality"]["queries"]
        ),
        "correction_suppression": tuple(
            str(row["case_key"])
            for row in generated_corpora["correction_suppression"]["cases"]
        ),
        "decision_recovery": tuple(
            str(row["query_key"])
            for row in generated_corpora["decision_recovery"]["queries"]
        ),
        "provenance_explanation": tuple(
            str(row["case_key"])
            for row in generated_corpora["provenance_explanation"]["memories"]
        ),
        "entity_resolution": tuple(
            str(row["group_key"])
            for row in generated_corpora["entity_resolution"]["groups"]
        ),
        "graph_hop_retrieval": tuple(
            str(row["group_key"])
            for row in generated_corpora["graph_hop_retrieval"]["groups"]
        ),
    }
    generated_corpus_digests = {
        suite_key: corpus["corpus_digest"]
        for suite_key, corpus in generated_corpora.items()
    }

    assert tuple(release_check.SEMANTIC_EVAL_CANONICAL_TARGETS) == VNEXT_EVAL_SUITE_ORDER
    assert release_check.SEMANTIC_EVAL_CANONICAL_TARGETS == VNEXT_ACCEPTANCE_TARGETS
    assert release_check.SEMANTIC_EVAL_CANONICAL_CASE_KEYS == generated_case_keys
    assert release_check.SEMANTIC_EVAL_CANONICAL_CORPUS_DIGESTS == generated_corpus_digests

    generator_contract = vnext_evals.canonical_semantic_eval_release_contract()
    assert release_check._generator_release_contract() == generator_contract
    for suite in canonical_semantic_eval_report["suites"]:
        suite_key = suite["suite_key"]
        assert suite["title"] == generator_contract[suite_key]["title"]
        assert set(suite["metrics"]) == release_check._SUITE_METRIC_KEYS[suite_key]
        canonical_cases = generator_contract[suite_key]["cases"]
        for case, canonical_case in zip(suite["cases"], canonical_cases, strict=True):
            assert set(case) == release_check._CASE_KEYS[suite_key]
            assert set(case["metrics"]) == release_check._CASE_METRIC_KEYS[suite_key]
            assert set(case["evidence"]) == release_check._CASE_EVIDENCE_KEYS[suite_key]
            if "query" in canonical_case:
                assert case["evidence"]["query"] == canonical_case["query"]
    retrieval_metrics = canonical_semantic_eval_report["suites"][0]["metrics"]
    assert set(retrieval_metrics["latency_ms"]) == {"p50", "p95", "max"}
    assert set(retrieval_metrics["subsets"]) == {"lexical_overlap", "paraphrase"}
    assert all(
        set(subset) == {"query_count", "recall_at_1", "recall_at_5", "mrr"}
        for subset in retrieval_metrics["subsets"].values()
    )
    assert set(retrieval_metrics["seeding"]) == {
        "seeded_memory_count",
        "embedded_memory_count",
        "embedding_signature",
        "embedding_note",
    }
    assert set(canonical_semantic_eval_report["suites"][2]["metrics"]["memory_types_filter"]) == {
        "available",
        "note",
    }
    for case in canonical_semantic_eval_report["suites"][5]["cases"]:
        assert set(case["evidence"]["winner_stage_ranks"]) == {"graph"}
        assert set(case["evidence"]["control_graph_stage"]) == {
            "status",
            "matched_entities",
            "candidate_count",
        }


def test_semantic_eval_digest_binds_nested_targets_metrics_cases_and_evidence(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    mutations = (
        lambda report: report["targets"]["graph_hop_retrieval"]["graph_lift"].update(
            {"minimum": 0.0}
        ),
        lambda report: report["suites"][0]["metrics"].update({"mrr": 0.0}),
        lambda report: report["suites"][0]["cases"][0]["metrics"].update(
            {"recall_at_5": 0.0}
        ),
        lambda report: report["suites"][0]["cases"][0]["evidence"][
            "top_memory_keys"
        ].append("fabricated-memory"),
    )
    for mutate in mutations:
        report = deepcopy(canonical_semantic_eval_report)
        mutate(report)
        issues = release_check.validate_semantic_eval_report(report)
        assert any("report_digest does not match" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("mutate", "expected_issue"),
    (
        pytest.param(
            lambda report: report["suites"][0]["metrics"].update(
                {"latency_ms": {"anything": ["goes"]}}
            ),
            "latency_ms",
            id="latency-object-shape",
        ),
        pytest.param(
            lambda report: report["suites"][0]["metrics"]["latency_ms"].update(
                {"invented": 1}
            ),
            "latency_ms",
            id="latency-extra-key",
        ),
        pytest.param(
            lambda report: report["suites"][0]["metrics"].update({"mrr": "anything"}),
            "mrr",
            id="suite-mrr-string",
        ),
        pytest.param(
            lambda report: report["suites"][0]["cases"][0]["metrics"].update(
                {"recall_at_1": "anything"}
            ),
            "recall_at_1",
            id="case-recall-string",
        ),
        pytest.param(
            lambda report: report["suites"][5]["cases"][0]["evidence"][
                "winner_stage_ranks"
            ].update({"invented": 99}),
            "winner_stage_ranks",
            id="graph-rank-extra-key",
        ),
        pytest.param(
            lambda report: report["suites"][0]["cases"][0]["evidence"].update(
                {"query": "invented query"}
            ),
            "canonical case",
            id="substituted-query",
        ),
        pytest.param(
            lambda report: report["suites"][0].update({"title": "Anything"}),
            "canonical suite title",
            id="substituted-title",
        ),
    ),
)
def test_semantic_eval_rejects_reviewer_nested_reproductions_with_fresh_digest(
    canonical_semantic_eval_report: dict[str, object],
    mutate,
    expected_issue: str,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    mutate(report)
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert issues
    assert any(expected_issue in issue for issue in issues), issues
    assert not any("report_digest does not match" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("mutate", "expected_issue"),
    (
        pytest.param(
            lambda report: report["targets"]["correction_suppression"][
                "pre_correction_visibility"
            ].update({"minimum": True}),
            "report targets",
            id="report-minimum-bool",
        ),
        pytest.param(
            lambda report: report["suites"][1]["targets"][
                "pre_correction_visibility"
            ].update({"minimum": True}),
            "suite 'correction_suppression' targets",
            id="suite-minimum-bool",
        ),
        pytest.param(
            lambda report: report["targets"]["provenance_explanation"][
                "orphan_provenance_count"
            ].update({"maximum": False}),
            "report targets",
            id="report-maximum-bool",
        ),
        pytest.param(
            lambda report: report["suites"][3]["targets"][
                "orphan_provenance_count"
            ].update({"maximum": False}),
            "suite 'provenance_explanation' targets",
            id="suite-maximum-bool",
        ),
    ),
)
def test_semantic_eval_rejects_boolean_canonical_targets_with_fresh_digest(
    canonical_semantic_eval_report: dict[str, object],
    mutate,
    expected_issue: str,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    mutate(report)
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any(expected_issue in issue for issue in issues), issues
    assert not any("report_digest does not match" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("path", "value"),
    (
        pytest.param(("generated_at",), "anything", id="invalid-generated-at"),
        pytest.param(("suites", 0, "metrics", "mrr"), True, id="suite-bool"),
        pytest.param(("suites", 0, "metrics", "mrr"), math.nan, id="suite-nan"),
        pytest.param(("suites", 0, "metrics", "mrr"), math.inf, id="suite-pos-inf"),
        pytest.param(("suites", 0, "metrics", "mrr"), -math.inf, id="suite-neg-inf"),
        pytest.param(
            ("suites", 0, "metrics", "latency_ms", "p50"),
            math.nan,
            id="nested-latency-nan",
        ),
        pytest.param(
            ("suites", 0, "cases", 0, "metrics", "recall_at_5"),
            True,
            id="retrieval-case-bool",
        ),
        pytest.param(
            ("suites", 0, "cases", 0, "metrics", "recall_at_1"),
            {},
            id="retrieval-case-unhashable-recall",
        ),
        pytest.param(
            ("suites", 0, "cases", 0, "evidence", "top_memory_keys"),
            [[]],
            id="unhashable-ranked-item",
        ),
        pytest.param(
            ("suites", 1, "cases", 0, "evidence", "original_memory_key"),
            {},
            id="unhashable-correction-key",
        ),
        pytest.param(
            ("suites", 3, "cases", 0, "metrics", "explain_complete"),
            True,
            id="provenance-case-bool",
        ),
        pytest.param(
            ("suites", 2, "cases", 0, "metrics", "recall_at_1"),
            [],
            id="decision-unhashable-recall-at-1",
        ),
        pytest.param(
            ("suites", 2, "cases", 0, "metrics", "recall_at_5"),
            {},
            id="decision-unhashable-recall-at-5",
        ),
        pytest.param(
            ("suites", 2, "cases", 0, "metrics", "filtered_recall_at_5"),
            [],
            id="decision-unhashable-filtered-recall",
        ),
        pytest.param(
            ("suites", 4, "cases", 0, "metrics", "resolved"),
            True,
            id="entity-case-bool",
        ),
        pytest.param(
            ("suites", 4, "cases", 0, "evidence", "aliases"),
            [{}],
            id="unhashable-alias-item",
        ),
        pytest.param(
            (
                "suites",
                5,
                "cases",
                0,
                "evidence",
                "control_graph_stage",
                "candidate_count",
            ),
            False,
            id="graph-control-bool",
        ),
        pytest.param(
            (
                "suites",
                5,
                "cases",
                0,
                "evidence",
                "control_graph_stage",
                "status",
            ),
            "disabled: fabricated",
            id="graph-control-status",
        ),
        pytest.param(
            ("suites", 5, "cases", 0, "metrics", "graph_recall_at_5"),
            {},
            id="graph-unhashable-recall",
        ),
        pytest.param(
            ("suites", 5, "cases", 0, "metrics", "fts_recall_at_5"),
            [],
            id="graph-unhashable-fts-recall",
        ),
        pytest.param(
            ("suites", 5, "cases", 0, "metrics", "winner_has_graph_rank"),
            {},
            id="graph-unhashable-winner-rank",
        ),
    ),
)
def test_semantic_eval_nested_types_fail_closed_with_fresh_digest(
    canonical_semantic_eval_report: dict[str, object],
    path: tuple[object, ...],
    value: object,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    target = report
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert issues
    assert not any("report_digest does not match" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("suite_index", "id_field"),
    (
        pytest.param(3, "memory_id", id="repeated-provenance-memory-id"),
        pytest.param(4, "entity_id", id="repeated-entity-id"),
    ),
)
def test_semantic_eval_rejects_repeated_evidence_ids(
    canonical_semantic_eval_report: dict[str, object],
    suite_index: int,
    id_field: str,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    cases = report["suites"][suite_index]["cases"]
    cases[1]["evidence"][id_field] = cases[0]["evidence"][id_field]
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("IDs must be present and distinct" in issue for issue in issues), issues


def test_semantic_eval_report_rejects_structurally_fabricated_evidence(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    report["targets"] = {}
    for suite in report["suites"]:
        suite["cases"] = [{"status": "pass"}]
        suite["metrics"]["target_checks"] = {"fabricated_check": "pass"}
    report["summary"].update(
        {
            "case_count": 6,
            "passed_case_count": 6,
            "failed_case_count": 0,
            "pass_rate": 1.0,
        }
    )
    report["provider_token"] = "hf_examplecredential123456789"
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("canonical acceptance targets" in issue for issue in issues), issues
    assert any("canonical case identities" in issue for issue in issues), issues
    assert any("canonical pass checks" in issue for issue in issues), issues
    assert any("credential-like material" in issue for issue in issues), issues
    assert any("unexpected provider_token" in issue for issue in issues), issues

    report_path = tmp_path / "semantic-eval-report.json"
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid semantic eval report"):
        release_check.write_semantic_eval_attestation(
            report_path=report_path,
            attestation_path=tmp_path / "semantic-eval-attestation.json",
            source_sha="a" * 40,
        )


def test_semantic_eval_report_rejects_substituted_case_and_check_even_with_fresh_digest(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    report["suites"][2]["cases"][0]["case_key"] = "decision-query-fabricated"
    report["suites"][5]["metrics"]["target_checks"] = {
        "fabricated_check": "pass"
    }
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("canonical case identities" in issue for issue in issues), issues
    assert any("canonical pass checks" in issue for issue in issues), issues


def test_semantic_artifacts_recursively_reject_secret_values_under_benign_keys(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    report["suites"][0]["cases"][0]["evidence"]["query"] = (
        "hf_examplecredential123456789"
    )
    _refresh_semantic_report_digest(report)
    report_issues = release_check.validate_semantic_eval_report(report)
    assert any("credential-like material" in issue for issue in report_issues), report_issues

    report_path = tmp_path / "semantic-eval-report.json"
    attestation_path = tmp_path / "semantic-eval-attestation.json"
    report_path.write_text(
        json.dumps(canonical_semantic_eval_report, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_check.write_semantic_eval_attestation(
        report_path=report_path,
        attestation_path=attestation_path,
        source_sha="a" * 40,
    )
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation["backend"] = "hf_examplecredential123456789"
    attestation_path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")
    attestation_issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )
    assert any(
        "credential-like material" in issue for issue in attestation_issues
    ), attestation_issues


def test_semantic_eval_attestation_binds_passing_report_to_exact_sha(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report_path = tmp_path / "semantic-eval-report.json"
    attestation_path = tmp_path / "semantic-eval-attestation.json"
    report_path.write_text(
        json.dumps(canonical_semantic_eval_report, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    source_sha = "a" * 40

    assert len(canonical_semantic_eval_report["suites"]) == 6
    assert canonical_semantic_eval_report["summary"]["case_count"] == 78
    assert release_check.validate_semantic_eval_report(
        canonical_semantic_eval_report
    ) == []
    release_check.write_semantic_eval_attestation(
        report_path=report_path,
        attestation_path=attestation_path,
        source_sha=source_sha,
    )
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert attestation["embedding_signature"] == canonical_semantic_eval_report["embedding_signature"]
    assert str(attestation["report_digest"]).startswith("sha256:")

    assert release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha=source_sha,
    ) == []
    assert any("exact release SHA" in issue for issue in release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="b" * 40,
    ))


def test_semantic_eval_report_accepts_live_off_window_reciprocal_ranks(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    retrieval = report["suites"][0]
    cases_by_key = {case["case_key"]: case for case in retrieval["cases"]}

    for case_key, reciprocal_rank in (
        ("paraphrase-004", 1 / 9),
        ("paraphrase-016", 1 / 10),
    ):
        case = cases_by_key[case_key]
        assert case["status"] == "fail"
        assert case["metrics"]["recall_at_1"] == 0.0
        assert case["metrics"]["recall_at_5"] == 0.0
        assert case["evidence"]["expected_memory_key"] not in case["evidence"][
            "top_memory_keys"
        ]
        case["metrics"]["reciprocal_rank"] = reciprocal_rank

    _reconcile_retrieval_mrr(report)
    _refresh_semantic_report_digest(report)

    assert release_check.validate_semantic_eval_report(report) == []


@pytest.mark.parametrize(
    "reciprocal_rank",
    (
        pytest.param(0.2, id="rank-above-off-window-maximum"),
        pytest.param(0.15, id="not-an-exact-reciprocal-rank"),
    ),
)
def test_semantic_eval_report_rejects_invalid_off_window_reciprocal_rank(
    canonical_semantic_eval_report: dict[str, object],
    reciprocal_rank: float,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    retrieval = report["suites"][0]
    missed_case = next(
        case for case in retrieval["cases"] if case["case_key"] == "paraphrase-004"
    )
    missed_case["metrics"]["reciprocal_rank"] = reciprocal_rank
    _reconcile_retrieval_mrr(report)
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("reciprocal of a rank from 6 through 10" in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("field", "value", "expected_issue"),
    (
        pytest.param(
            "recall_at_5",
            1.0,
            "recall_at_5 does not match the target rank",
            id="inconsistent-recall",
        ),
        pytest.param(
            "status",
            "pass",
            "status does not match recall_at_5",
            id="inconsistent-status",
        ),
    ),
)
def test_semantic_eval_report_rejects_off_window_recall_or_status_mismatch(
    canonical_semantic_eval_report: dict[str, object],
    field: str,
    value: object,
    expected_issue: str,
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    retrieval = report["suites"][0]
    missed_case = next(
        case for case in retrieval["cases"] if case["case_key"] == "paraphrase-004"
    )
    missed_case["metrics"]["reciprocal_rank"] = 1 / 9
    if field == "status":
        missed_case["status"] = value
    else:
        missed_case["metrics"][field] = value
    _reconcile_retrieval_mrr(report)
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any(expected_issue in issue for issue in issues), issues


def test_semantic_eval_report_accepts_honest_case_misses_with_passing_suite_targets(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)

    decision = report["suites"][2]
    decision_case = decision["cases"][0]
    decision_case["status"] = "fail"
    decision_case["metrics"].update(
        {
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
            "reciprocal_rank": 0.0,
            "filtered_recall_at_5": 0.0,
            "filtered_reciprocal_rank": 0.0,
        }
    )
    decision_case["evidence"]["top_memory_keys"] = []
    decision_case["evidence"]["filtered_top_memory_keys"] = []
    decision["metrics"].update(
        {
            "decision_recall_at_1": 0.9,
            "decision_recall_at_5": 0.9,
            "decision_mrr": 0.9,
            "filtered_decision_recall_at_5": 0.9,
            "filtered_decision_mrr": 0.9,
        }
    )

    graph = report["suites"][5]
    graph_case = graph["cases"][0]
    graph_case["status"] = "fail"
    graph_case["metrics"].update(
        {
            "graph_recall_at_5": 0.0,
            "winner_has_graph_rank": 0.0,
        }
    )
    graph_case["evidence"]["graph_top_keys"] = []
    graph_case["evidence"]["winner_stage_ranks"] = {}
    graph["metrics"].update(
        {
            "graph_recall_at_5": 0.8,
            "graph_lift": 0.8,
            "winner_graph_rank_rate": 0.8,
        }
    )

    report["summary"].update(
        {
            "passed_case_count": 73,
            "failed_case_count": 5,
            "pass_rate": 73 / 78,
        }
    )
    _refresh_semantic_report_digest(report)

    assert release_check.validate_semantic_eval_report(report) == []


def test_semantic_eval_report_accepts_one_correction_replacement_miss(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    correction = report["suites"][1]
    missed_case = correction["cases"][0]
    missed_case["status"] = "fail"
    missed_case["metrics"].update(
        {
            "replacement_recall_at_5": 0.0,
            "replacement_reciprocal_rank": 0.0,
        }
    )
    missed_case["evidence"]["post_correction_top_keys"] = []
    correction["metrics"].update(
        {
            "replacement_recall_at_5": 5 / 6,
            "replacement_mrr": 5 / 6,
        }
    )
    report["summary"].update(
        {
            "passed_case_count": 74,
            "failed_case_count": 4,
            "pass_rate": 74 / 78,
        }
    )
    _refresh_semantic_report_digest(report)

    assert release_check.validate_semantic_eval_report(report) == []


def test_semantic_eval_report_rejects_correction_replacement_recall_below_target(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    correction = report["suites"][1]
    for missed_case in correction["cases"][:2]:
        missed_case["status"] = "fail"
        missed_case["metrics"].update(
            {
                "replacement_recall_at_5": 0.0,
                "replacement_reciprocal_rank": 0.0,
            }
        )
        missed_case["evidence"]["post_correction_top_keys"] = []
    correction["metrics"].update(
        {
            "replacement_recall_at_5": 4 / 6,
            "replacement_mrr": 4 / 6,
        }
    )
    correction["metrics"]["target_checks"]["replacement_recall_at_5"] = "fail"
    correction["status"] = "fail"
    report["status"] = "fail"
    report["summary"].update(
        {
            "status": "fail",
            "passed_case_count": 73,
            "failed_case_count": 5,
            "pass_rate": 73 / 78,
        }
    )
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert issues
    assert any("replacement_recall_at_5 is below its canonical minimum" in issue for issue in issues)
    assert any("contains a failed target check" in issue for issue in issues)


def test_semantic_eval_report_accepts_production_graph_winner_stage_ranks(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    graph_case = report["suites"][5]["cases"][0]
    graph_case["evidence"]["winner_stage_ranks"] = {
        "fts": 1,
        "vector": 2,
        "graph": 3,
        "temporal_anchor": 4,
    }
    _refresh_semantic_report_digest(report)

    assert release_check.validate_semantic_eval_report(report) == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("vector_candidate_count", 48.0),
        ("vector_stage_participated", 1),
        ("vector_stage_participated", 1.0),
        ("paraphrase_recall_at_5", 1),
        ("paraphrase_recall_at_5", True),
    ),
    ids=(
        "candidate-int-to-float",
        "participated-bool-to-int",
        "participated-bool-to-float",
        "paraphrase-float-to-int",
        "paraphrase-float-to-bool",
    ),
)
def test_semantic_eval_attestation_rejects_reviewer_type_substitutions(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
    field: str,
    replacement: object,
) -> None:
    _report_path, attestation_path = _write_semantic_eval_artifact_pair(
        tmp_path=tmp_path,
        report=canonical_semantic_eval_report,
    )
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation[field] = replacement
    attestation_path.write_text(
        json.dumps(attestation, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )

    assert any(field in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("vector_candidate_count", 0, "must be >= 1"),
        ("vector_candidate_count", -1, "must be >= 1"),
        ("vector_candidate_count", True, "must be an integer"),
        ("vector_stage_participated", "true", "must be a boolean"),
        ("vector_stage_participated", False, "must be true"),
        ("paraphrase_recall_at_5", 1, "must be a finite float"),
        ("paraphrase_recall_at_5", True, "must be a finite float"),
        ("paraphrase_recall_at_5", math.nan, "must be a finite float"),
        ("paraphrase_recall_at_5", math.inf, "must be a finite float"),
        ("paraphrase_recall_at_5", -math.inf, "must be a finite float"),
        ("paraphrase_recall_at_5", -0.1, "must be between 0.0 and 1.0"),
        ("paraphrase_recall_at_5", 1.1, "must be between 0.0 and 1.0"),
    ),
)
def test_semantic_eval_attestation_validates_copied_summary_scalar_shapes(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
    field: str,
    replacement: object,
    message: str,
) -> None:
    _report_path, attestation_path = _write_semantic_eval_artifact_pair(
        tmp_path=tmp_path,
        report=canonical_semantic_eval_report,
    )
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation[field] = replacement
    attestation_path.write_text(
        json.dumps(attestation, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )

    assert any(message in issue for issue in issues), issues


def test_semantic_eval_attestation_recursively_rejects_all_copied_field_type_drift(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    _report_path, attestation_path = _write_semantic_eval_artifact_pair(
        tmp_path=tmp_path,
        report=canonical_semantic_eval_report,
    )
    canonical_attestation = json.loads(
        attestation_path.read_text(encoding="utf-8")
    )
    copied_fields = (
        "report_digest",
        "generated_at",
        "suite",
        "status",
        "embedding_signature",
        "backend",
        "retrieval_mode",
        "vector_candidate_count",
        "vector_stage_participated",
        "paraphrase_recall_at_5",
    )
    type_representatives: tuple[object, ...] = (
        None,
        False,
        0,
        0.0,
        "type-drift",
        [],
        {"type": "drift"},
    )
    checked = 0

    for field in copied_fields:
        canonical_value = canonical_attestation[field]
        for path, node in _json_nodes(canonical_value):
            for replacement in type_representatives:
                if type(replacement) is type(node):
                    continue
                mutated = deepcopy(canonical_attestation)
                mutated[field] = _replace_json_node(
                    canonical_value,
                    path=path,
                    replacement=replacement,
                )
                attestation_path.write_text(
                    json.dumps(mutated, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                issues = release_check.validate_semantic_eval_attestation(
                    attestation_path=attestation_path,
                    expected_sha="a" * 40,
                )

                assert any(field in issue for issue in issues), (
                    field,
                    path,
                    node,
                    replacement,
                    issues,
                )
                checked += 1

    assert checked == 102


def test_real_no_provider_release_report_fails_closed_without_digest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VNEXT_EVAL_DATABASE_URL_ENV, "sqlite:///:memory:")
    for key in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    report = run_vnext_evals(suite="all", release_gate=True)
    issues = release_check.validate_semantic_eval_report(report)

    assert report["status"] == "fail"
    assert report["report_digest"] == release_check._semantic_eval_report_digest(report)
    assert issues
    assert not any("report_digest does not match" in issue for issue in issues), issues


def test_generator_release_contract_does_not_resolve_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_provider_lookup() -> None:
        raise AssertionError("canonical release contract must be provider-free")

    monkeypatch.setattr(vnext_evals, "get_embedding_provider", unexpected_provider_lookup)

    contract = vnext_evals.canonical_semantic_eval_release_contract()

    assert tuple(contract) == VNEXT_EVAL_SUITE_ORDER


def test_semantic_eval_report_rejects_no_vector_participation_skips_and_credentials(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    retrieval = report["suites"][0]
    retrieval["metrics"]["vector_candidate_count"] = 0
    retrieval["metrics"]["vector_stage_participated"] = False
    report["suites"][-1]["status"] = "skipped"
    report["suites"][-1]["cases"] = []
    report["summary"]["executed_suite_count"] = 5
    report["summary"]["skipped_suite_count"] = 1
    report["skipped_suites"] = [
        {"suite_key": "graph_hop_retrieval", "reason": "provider unavailable"}
    ]
    report["provider_api_key"] = "sk-not-safe-to-attest"
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("without skips" in issue for issue in issues)
    assert any("vector_candidate_count" in issue for issue in issues)
    assert any("credential-like" in issue for issue in issues)


def test_semantic_eval_report_rejects_empty_cases(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    report["suites"][0]["cases"] = []
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("nonempty cases" in issue for issue in issues), issues
    assert any("case_count" in issue for issue in issues), issues


def test_semantic_eval_report_reconciles_all_summary_fields(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    summary = report["summary"]
    summary.update(
        {
            "status": "fail",
            "suite_count": 99,
            "executed_suite_count": 5,
            "skipped_suite_count": 1,
            "case_count": 99,
            "passed_case_count": 98,
            "failed_case_count": 1,
            "pass_rate": 0.5,
            "suite_order": list(reversed(summary["suite_order"])),
        }
    )
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    for field in (
        "suite_count",
        "executed_suite_count",
        "skipped_suite_count",
        "case_count",
        "passed_case_count",
        "failed_case_count",
        "pass_rate",
        "suite_order",
        "summary status",
    ):
        assert any(field in issue for issue in issues), (field, issues)


def test_semantic_eval_report_rejects_inconsistent_case_status_and_failed_target_checks(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report = deepcopy(canonical_semantic_eval_report)
    report["suites"][0]["cases"][0]["status"] = "fail"
    report["suites"][1]["metrics"]["target_checks"]["suppression_rate"] = "fail"
    _refresh_semantic_report_digest(report)

    issues = release_check.validate_semantic_eval_report(report)

    assert any("status does not match recall_at_5" in issue for issue in issues), issues
    assert any("failed target check" in issue for issue in issues), issues
    assert any("derived" in issue for issue in issues), issues


def test_semantic_eval_report_rejects_missing_fingerprint_and_raw_url(
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    missing = deepcopy(canonical_semantic_eval_report)
    del missing["embedding_signature"]["endpoint_fingerprint"]
    _refresh_semantic_report_digest(missing)
    missing_issues = release_check.validate_semantic_eval_report(missing)
    assert any("contain exactly" in issue for issue in missing_issues), missing_issues
    assert any("endpoint_fingerprint" in issue for issue in missing_issues), missing_issues

    raw_url = deepcopy(canonical_semantic_eval_report)
    raw_url["embedding_signature"]["model"] = "https://models.example.invalid/private"
    raw_url["embedding_signature"]["model_fingerprint"] = sha256(
        raw_url["embedding_signature"]["model"].encode("utf-8")
    ).hexdigest()
    _refresh_semantic_report_digest(raw_url)
    raw_url_issues = release_check.validate_semantic_eval_report(raw_url)
    assert any("raw URL" in issue for issue in raw_url_issues), raw_url_issues


def test_semantic_eval_attestation_rejects_fingerprint_drift(
    tmp_path: Path,
    canonical_semantic_eval_report: dict[str, object],
) -> None:
    report_path = tmp_path / "semantic-eval-report.json"
    attestation_path = tmp_path / "semantic-eval-attestation.json"
    report = deepcopy(canonical_semantic_eval_report)
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    release_check.write_semantic_eval_attestation(
        report_path=report_path,
        attestation_path=attestation_path,
        source_sha="a" * 40,
    )

    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation["embedding_signature"]["endpoint_fingerprint"] = "fedcba9876543210"
    attestation_path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")
    issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )
    assert any("embedding_signature does not match" in issue for issue in issues), issues

    report["embedding_signature"]["endpoint_fingerprint"] = "0011223344556677"
    _refresh_semantic_report_digest(report)
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    report_drift_issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )
    assert any("report SHA-256" in issue for issue in report_drift_issues), report_drift_issues
    assert any(
        "embedding_signature does not match" in issue for issue in report_drift_issues
    ), report_drift_issues


def test_semantic_artifact_validation_rejects_malformed_report_and_attestation(
    tmp_path: Path,
) -> None:
    malformed_report = {"schema_version": "unknown", "status": "pass"}
    report_issues = release_check.validate_semantic_eval_report(malformed_report)
    assert any("unsupported schema_version" in issue for issue in report_issues)
    assert any("missing summary" in issue for issue in report_issues)
    assert any("missing suites" in issue for issue in report_issues)

    attestation_path = tmp_path / "semantic-eval-attestation.json"
    attestation_path.write_text("{not-json}\n", encoding="utf-8")
    attestation_issues = release_check.validate_semantic_eval_attestation(
        attestation_path=attestation_path,
        expected_sha="a" * 40,
    )
    assert any("could not read semantic eval attestation" in issue for issue in attestation_issues)
