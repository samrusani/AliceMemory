"""Write the synthetic company notes.

    python scripts/generate_showcase_company.py --size 1000 --out DIR
    python scripts/generate_showcase_company.py --size 10000 --out DIR

The same seed writes the same files. The default seed is the one the tests use.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.showcase_company.company import DEFAULT_SEED, SIZES, generate, write_notes  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, required=True, choices=SIZES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    notes = generate(args.size, seed=args.seed)
    write_notes(notes, args.out)
    print(f"wrote {len(notes)} notes of {args.out} at seed {args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
