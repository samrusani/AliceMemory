"""Fuzz the Hermes config.yaml writer against PyYAML, the loader Hermes uses.

install --host hermes edits ~/.hermes/config.yaml as text (see
plan_hermes_config in alicebot_api.host_install). This script generates
configs built from the YAML the writer claims to handle, then mutates them
line by line into YAML it may not handle, and checks one property on every
case PyYAML can load:

- the writer refuses the file (HermesConfigRefused), or
- the written text loads in PyYAML, every key outside mcp_servers.alice
  reads exactly as before, and mcp_servers.alice points at the data dir.

A generated config must never be refused. A mutant may be. Two mutations
target anchors: one puts an ``&anchor`` inside an old alice block and an
alias to it outside the block (the writer must refuse, or the alias would
be left undefined), the other puts an alias inside alice to an anchor
defined outside it (the writer may replace alice).

Run from a checkout with the dev extra installed:

    PYTHONPATH=apps/api/src:workers python scripts/fuzz_hermes_config_writer.py --seeds 300

tests/unit/test_hermes_config_install.py runs a short seeded pass on every
unit run.
"""

from __future__ import annotations

import argparse
import copy
import random
import sys
from dataclasses import dataclass, field
from ntpath import basename as _windows_basename

import yaml

from alicebot_api.host_install import (
    ALICE_MEMORY_DATA_DIR_ENV,
    HermesConfigRefused,
    plan_hermes_config,
)

BS = chr(92)  # one backslash, spelled so no escape processing can touch it
OLD_VAULT = "/old/vault"
DEFAULT_DATA_DIR = "/fuzz/alice-vault"

SCALARS = [
    "gpt-4o",
    "gpt-4o  # inline comment",
    "yes",
    "no",
    "on",
    "off",
    "true",
    "null",
    "~",
    "42",
    "0o17",
    "1_000",
    "2026-09-22",
    "12:30",
    "http://localhost:8080/path",
    '"tab' + BS + "t and " + BS + "u00e9 and " + BS + '"quote' + BS + '""',
    "'single '' quote # not a comment'",
    "'C:" + BS + "work'",
    '"# hash in quotes"',
    "[a, 'b, c', \"d\"]",
    "{x: 1, y: [2, 3]}",
]
BLOCK_HEADERS = ("|", "|-", "|+", ">", ">-", "|2")
MUTANT_LINES = [
    'note: "multi',
    'line quoted"',
    "args: [a,",
    "  b]",
    "mcp_servers:",
    "  alice: {}",
    "- item",
    "? key",
    ": value",
    "key: &anchor",
    "other: *anchor",
    "<<: *anchor",
    "  # indented comment",
    "\t- tab",
    "key:\tvalue",
    "---",
    "...",
    "block: |",
    "  |",
    "  text",
    "",
    "   ",
    "key: 'it''s'",
    "k: v # c",
    "k: v#notcomment",
    '"quoted key": 1',
    "'mcp_servers': {}",
    "mcp_servers: {}",
    "  mcp_servers: nested",
]


class FuzzFailure(AssertionError):
    """The writer produced text that changes the meaning of the original."""


def gen_scalar(rng: random.Random) -> str:
    return rng.choice(SCALARS)


def gen_block_scalar(rng: random.Random, indent: int) -> list[str]:
    header = rng.choice(BLOCK_HEADERS)
    pad = " " * (indent + 2)
    body = [
        pad + "line one",
        pad + "mcp_servers: this is scalar text",
        pad + "- not a list item",
        pad + "# not a comment",
    ]
    if rng.random() < 0.5:
        body.insert(2, "")
    if rng.random() < 0.5:
        body += ["", ""]
    return [header, *body]


def gen_value_lines(rng: random.Random, key: str, indent: int, depth: int) -> list[str]:
    pad = " " * indent
    roll = rng.random()
    if roll < 0.35 or depth > 2:
        return [f"{pad}{key}: {gen_scalar(rng)}"]
    if roll < 0.5:
        header, *body = gen_block_scalar(rng, indent)
        return [f"{pad}{key}: {header}", *body]
    if roll < 0.7:
        step = rng.choice([2, 4])
        lines = [f"{pad}{key}:"]
        for i in range(rng.randint(1, 3)):
            lines += gen_value_lines(rng, f"k{depth}{i}", indent + step, depth + 1)
        return lines
    compact = rng.random() < 0.5
    item_pad = pad if compact else pad + "  "
    lines = [f"{pad}{key}:"]
    for i in range(rng.randint(1, 3)):
        if rng.random() < 0.5:
            lines.append(f"{item_pad}- {gen_scalar(rng)}")
        else:
            lines.append(f"{item_pad}- name: item{i}")
            lines.append(f"{item_pad}  enabled: {rng.choice(['yes', 'no', 'true'])}")
    return lines


def gen_old_alice(rng: random.Random, pad: str) -> list[str]:
    """An alice entry as v0.16.0 or a hand edit might have left it."""

    style = rng.choice(["block", "block", "flow-args", "flow"])
    if style == "flow":
        return [
            f"{pad}alice: {{command: uvx, args: [alice-memory, mcp, --data-dir, {OLD_VAULT}]}}"
        ]
    if style == "flow-args":
        return [
            f"{pad}alice:",
            f"{pad}  command: uvx",
            f"{pad}  args: [alice-memory, mcp, --data-dir, {OLD_VAULT}]",
        ]
    return [
        f"{pad}alice:",
        f"{pad}  command: uvx",
        f"{pad}  args:",
        f"{pad}    - alice-memory",
        f"{pad}    - mcp",
        f'{pad}    - "--data-dir"',
        f"{pad}    - {OLD_VAULT}",
    ]


def gen_mcp_servers(rng: random.Random) -> tuple[list[str], str]:
    kind = rng.choice(["children", "children", "children", "empty", "flow-empty", "null"])
    if kind == "empty":
        return [rng.choice(["mcp_servers:", "mcp_servers:  # none yet"])], kind
    if kind == "flow-empty":
        return [rng.choice(["mcp_servers: {}", "mcp_servers: {}  # none"])], kind
    if kind == "null":
        return [rng.choice(["mcp_servers: ~", "mcp_servers: null"])], kind
    step = rng.choice([2, 4])
    pad = " " * step
    lines = ["mcp_servers:"]
    names = [f"server{i}" for i in range(rng.randint(1, 3))]
    if rng.random() < 0.4:
        names.insert(rng.randint(0, len(names)), "alice")
    for name in names:
        if name == "alice":
            lines += gen_old_alice(rng, pad)
            continue
        if rng.random() < 0.3:
            lines.append(f'{pad}{name}: {{command: node, args: ["{name}.js"]}}')
        else:
            lines.append(f"{pad}{name}:")
            lines += gen_value_lines(rng, "command", step * 2, 3)
            lines += gen_value_lines(rng, "args", step * 2, 1)
            if rng.random() < 0.3:
                header, *body = gen_block_scalar(rng, step * 2)
                lines += [f"{pad * 2}notes: {header}", *body]
        if rng.random() < 0.3:
            lines.append(f"{pad}# a comment between servers")
    if rng.random() < 0.3:
        lines.append(f"{pad}# disabled:")
        lines.append(f"{pad}#   command: old")
    return lines, kind


def generate_config(rng: random.Random) -> tuple[str, str]:
    """A config built only from YAML the writer claims to handle, and its mcp_servers kind."""

    entries: list[list[str]] = []
    for i in range(rng.randint(0, 5)):
        entries.append(gen_value_lines(rng, f"key{i}", 0, 0))
    mcp_kind = "absent"
    if rng.random() < 0.75:
        block, mcp_kind = gen_mcp_servers(rng)
        entries.insert(rng.randint(0, len(entries)), block)
    lines: list[str] = []
    if rng.random() < 0.3:
        lines.append("# header comment")
    if rng.random() < 0.15:
        lines.append("---")
    for entry in entries:
        if rng.random() < 0.3:
            lines.append("")
        if rng.random() < 0.3:
            lines.append("# comment before an entry")
        lines += entry
    newline = "\r\n" if rng.random() < 0.15 else "\n"
    text = newline.join(lines)
    ends_in_block = bool(lines) and lines[-1].startswith(" ") and any(
        line.rstrip().endswith(BLOCK_HEADERS) for line in lines[-8:]
    )
    if lines and (rng.random() < 0.85 or ends_in_block):
        text += newline
    return text, mcp_kind


def _anchor_inside_alice(text: str) -> str | None:
    """Put an anchor on a node inside the old alice entry and alias it outside."""

    for old, new in (
        ("{command: uvx,", "{command: &fzin uvx,"),
        ("args: [alice-memory,", "args: [&fzin alice-memory,"),
        ("- alice-memory", "- &fzin alice-memory"),
        ("command: uvx", "command: &fzin uvx"),
    ):
        if old in text:
            mutated = text.replace(old, new, 1)
            newline = "\r\n" if "\r\n" in text else "\n"
            if not mutated.endswith(newline):
                mutated += newline
            return mutated + "fuzz_alias_of_alice: *fzin" + newline
    return None


def _alias_inside_alice(text: str) -> str | None:
    """Point alice's command at an anchor defined before it, outside alice."""

    if "command: uvx" not in text:
        return None
    newline = "\r\n" if "\r\n" in text else "\n"
    mutated = text.replace("command: uvx", "command: *fzout", 1)
    anchor_line = "fuzz_anchor_home: &fzout uvx" + newline
    if mutated.startswith("---" + newline):
        return "---" + newline + anchor_line + mutated[len("---" + newline):]
    return anchor_line + mutated


def mutate(rng: random.Random, text: str) -> tuple[str, str]:
    """One mutant of ``text`` and the name of the mutation that made it."""

    roll = rng.random()
    if roll < 0.08:
        mutated = _anchor_inside_alice(text)
        if mutated is not None:
            return mutated, "anchor-inside-alice"
    elif roll < 0.16:
        mutated = _alias_inside_alice(text)
        if mutated is not None:
            return mutated, "alias-inside-alice"
    lines = text.split("\n")
    position = rng.randint(0, len(lines))
    op = rng.random()
    if op < 0.5:
        lines.insert(position, rng.choice(MUTANT_LINES))
        return "\n".join(lines), "insert-line"
    if op < 0.75 and lines:
        del lines[min(position, len(lines) - 1)]
        return "\n".join(lines), "delete-line"
    if lines:
        index = min(position, len(lines) - 1)
        lines[index] = " " * rng.randint(0, 6) + lines[index].lstrip(" ")
    return "\n".join(lines), "reindent-line"


def _without_alice(loaded: object) -> dict:
    doc = copy.deepcopy(loaded) if isinstance(loaded, dict) else {}
    servers = doc.get("mcp_servers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers.pop("alice", None)
    doc["mcp_servers"] = servers
    return doc


def _check_alice(alice: object, data_dir: str) -> str | None:
    """None when ``alice`` is an entry that starts Alice on ``data_dir``, else why not."""

    if not isinstance(alice, dict):
        return "mcp_servers.alice is not a mapping"
    if not set(alice) <= {"command", "args", "env"}:
        return f"unexpected keys {sorted(set(alice))}"
    command = alice.get("command")
    if not isinstance(command, str) or _windows_basename(command).lower() not in {"uvx", "uvx.exe"}:
        return f"command {command!r} is not uvx"
    args = alice.get("args")
    if not isinstance(args, list) or "mcp" not in args:
        return f"args {args!r} do not run alice-memory mcp"
    try:
        index = args.index("--data-dir")
    except ValueError:
        return f"args {args!r} have no --data-dir"
    if index + 1 >= len(args) or args[index + 1] != data_dir:
        return f"args {args!r} do not point at {data_dir}"
    if alice.get("env") != {ALICE_MEMORY_DATA_DIR_ENV: data_dir}:
        return f"env {alice.get('env')!r} does not point at {data_dir}"
    return None


def check(text: str, data_dir: str, *, allow_refusal: bool) -> str:
    """Return "invalid", "refused" or "written"; raise FuzzFailure on a meaning change."""

    try:
        before = yaml.safe_load(text)
    except yaml.YAMLError:
        return "invalid"
    try:
        planned = plan_hermes_config(text, data_dir)
    except HermesConfigRefused:
        if not allow_refusal:
            raise
        return "refused"
    after_text = text if planned is None else planned
    try:
        after = yaml.safe_load(after_text)
    except yaml.YAMLError as exc:
        raise FuzzFailure(f"written text does not load: {exc}\n--- original\n{text}") from exc
    if not isinstance(after, dict):
        raise FuzzFailure(f"written text is not a mapping\n--- original\n{text}")
    if _without_alice(after) != _without_alice(before):
        raise FuzzFailure(f"a key outside alice changed\n--- original\n{text}\n--- written\n{after_text}")
    problem = _check_alice((after.get("mcp_servers") or {}).get("alice"), data_dir)
    if problem is not None:
        raise FuzzFailure(f"{problem}\n--- original\n{text}\n--- written\n{after_text}")
    return "written"


@dataclass
class FuzzCounts:
    generated_written: int = 0
    mutants_invalid: int = 0
    mutants_refused: int = 0
    mutants_written: int = 0
    by_mutation: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def mutants_valid(self) -> int:
        return self.mutants_refused + self.mutants_written

    def record(self, mutation: str, outcome: str) -> None:
        if outcome == "invalid":
            self.mutants_invalid += 1
        elif outcome == "refused":
            self.mutants_refused += 1
        else:
            self.mutants_written += 1
        per = self.by_mutation.setdefault(mutation, {})
        per[outcome] = per.get(outcome, 0) + 1


def run(
    seeds: range,
    *,
    configs_per_seed: int = 100,
    mutants_per_config: int = 6,
    data_dir: str = DEFAULT_DATA_DIR,
) -> FuzzCounts:
    """Fuzz the writer; raise FuzzFailure on the first case that changes meaning."""

    counts = FuzzCounts()
    for seed in seeds:
        rng = random.Random(seed)
        for _ in range(configs_per_seed):
            text, _kind = generate_config(rng)
            if check(text, data_dir, allow_refusal=False) == "written":
                counts.generated_written += 1
            for _ in range(mutants_per_config):
                mutant, mutation = mutate(rng, text)
                counts.record(mutation, check(mutant, data_dir, allow_refusal=True))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=40, help="Seeds 0..N-1. Default 40.")
    parser.add_argument("--configs-per-seed", type=int, default=100)
    parser.add_argument("--mutants-per-config", type=int, default=6)
    args = parser.parse_args(argv)
    try:
        counts = run(
            range(args.seeds),
            configs_per_seed=args.configs_per_seed,
            mutants_per_config=args.mutants_per_config,
        )
    except FuzzFailure as failure:
        print(f"FAIL: {failure}")
        return 1
    print(f"generated configs written, meaning kept: {counts.generated_written}")
    print(
        f"valid mutants: {counts.mutants_valid} "
        f"(written, meaning kept: {counts.mutants_written}; refused: {counts.mutants_refused})"
    )
    print(f"invalid mutants skipped: {counts.mutants_invalid}")
    for mutation, outcomes in sorted(counts.by_mutation.items()):
        print(f"  {mutation}: {outcomes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
