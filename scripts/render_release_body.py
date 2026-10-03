#!/usr/bin/env python3
"""Render a publication-neutral GitHub Release body from verified fields."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


_TAG_PATTERN = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")
_SHA256_LINE = re.compile(
    r"(?P<digest>[0-9a-f]{64})  (?P<filename>[A-Za-z0-9][A-Za-z0-9_.+-]*)"
)


def render_release_body(
    *,
    repository: str,
    tag: str,
    commit_sha: str,
    checksum_manifest: Path,
) -> str:
    """Return deterministic release prose that is valid before and after publication."""

    match = _TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError("tag must be a stable vX.Y.Z tag")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("commit SHA must be a 40-character lowercase hexadecimal digest")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must use owner/name syntax")

    artifact_lines: list[str] = []
    for line_number, raw_line in enumerate(
        checksum_manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        checksum = _SHA256_LINE.fullmatch(line)
        if checksum is None:
            raise ValueError(f"invalid checksum manifest line {line_number}")
        artifact_lines.append(
            f"- `{checksum.group('filename')}`: `sha256:{checksum.group('digest')}`"
        )
    if not artifact_lines:
        raise ValueError("checksum manifest contains no artifacts")

    version = match.group("version")
    repository_url = f"https://github.com/{repository}"
    return "\n".join(
        (
            f"Alice {tag}",
            "",
            "This release is identified by the immutable source and artifact records below.",
            "",
            "## Release identity",
            "",
            f"- Version: `{version}`",
            f"- Tag: [`{tag}`]({repository_url}/tree/{tag})",
            f"- Source commit: [`{commit_sha}`]({repository_url}/commit/{commit_sha})",
            f"- Package: [alice-memory {version}](https://pypi.org/project/alice-memory/{version}/)",
            "",
            "## Verified artifacts",
            "",
            *artifact_lines,
            "",
            "The attached `SHA256SUMS` file is the machine-readable checksum manifest.",
            f"Detailed change notes remain versioned in `docs/release/{tag}-release-notes.md`.",
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--checksums", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    body = render_release_body(
        repository=args.repository,
        tag=args.tag,
        commit_sha=args.commit_sha,
        checksum_manifest=args.checksums,
    )
    args.output.write_text(body, encoding="utf-8")
    print(f"Release body: PASS ({args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
