#!/usr/bin/env python3
"""Generate a deterministic synthetic Markdown folder for import timing.

The corpus is fully synthetic: every paragraph carries a unique token built
from the seed and its position, so duplicate collapse can never hide work.
The shape mirrors a large notes folder: about half plain paragraphs, plus
headings, lists and code fences in realistic proportions. Determinism comes
from a seeded PRNG, not from the clock or the machine.

Usage:
    python scripts/generate_markdown_perf_corpus.py --out DIR --files 13400 --seed 5

Nothing is read from or written to the network, and no real names are used.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from random import Random

WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu", "note", "draft", "review", "plan", "summary",
    "archive", "inbox", "index", "catalog", "ledger", "journal", "digest",
]

TOPICS = ["planning", "meeting notes", "research log", "checklist", "design review", "weekly digest"]


def _paragraph(rng: Random, token: str, sentences: int) -> str:
    words = [rng.choice(WORDS) for _ in range(sentences * 12)]
    lines = []
    for i in range(sentences):
        chunk = " ".join(words[i * 12 : (i + 1) * 12])
        lines.append(f"The {chunk} (ref {token}).")
    return " ".join(lines)


def _file_text(rng: Random, file_index: int, token_prefix: str) -> str:
    # ~11.5 KB per file at 12 paragraphs of 4 sentences.
    parts = [f"# {rng.choice(TOPICS).title()} {file_index:05d}", ""]
    kind_plan = [rng.choice(["para", "para", "heading", "list", "code", "para", "heading", "para", "list", "para", "code", "para", "para", "para", "heading", "para", "list", "para", "para", "para"]) for _ in range(20)]
    paragraph_index = 0
    for i, kind in enumerate(kind_plan):
        if kind == "heading":
            parts.append(f"## Section {i} {rng.choice(WORDS).title()}")
            parts.append("")
        elif kind == "list":
            parts.append(f"- Item {i} with {rng.choice(WORDS)} detail and {rng.choice(WORDS)} follow-up.")
            parts.append(f"- Item {i}b with {rng.choice(WORDS)} detail.")
            parts.append(f"- Item {i}c with {rng.choice(WORDS)} detail.")
            parts.append("")
        elif kind == "code":
            parts.append("```text")
            parts.append(f"step {i:02d} of file {file_index:05d} done")
            parts.append(f"log line {i} for file {file_index:05d}")
            parts.append("```")
            parts.append("")
        else:
            token = f"{token_prefix}p{paragraph_index:04d}"
            paragraph_index += 1
            parts.append(_paragraph(rng, token, 8))
            parts.append("")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic markdown corpus folder.")
    parser.add_argument("--out", required=True, type=Path, help="Directory to create and fill.")
    parser.add_argument("--files", type=int, default=13400, help="Number of markdown files.")
    parser.add_argument("--seed", type=int, default=20261027, help="Seed for the deterministic PRNG.")
    parser.add_argument("--subdirs", type=int, default=100, help="Number of subdirectories to spread files over.")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    rng = Random(args.seed)
    total = 0
    for file_index in range(args.files):
        subdir = args.out / f"sub{file_index % args.subdirs:03d}"
        subdir.mkdir(exist_ok=True)
        text = _file_text(rng, file_index, f"tok{args.seed:x}f{file_index:05d}")
        (subdir / f"note_{file_index:05d}.md").write_text(text, encoding="utf-8")
        total += len(text.encode("utf-8"))
    print(f"files={args.files} bytes={total} seed={args.seed} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
