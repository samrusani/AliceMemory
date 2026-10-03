"""Fuzz the Codex config.toml writer against an independent judge.

plan_codex_config edits ~/.codex/config.toml as text. This script builds
configs in the shapes the writer claims to accept, stamps every comment,
and checks the judge in tests/unit/toml_judge.py. A generated config must
not be refused, except a label in the judge's refusable set. A mutant may be.

Run from a checkout:

    PYTHONPATH=apps/api/src:workers python scripts/fuzz_codex_config_writer.py --seeds 20
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(ROOT / "workers"))

from alicebot_api.host_install import CodexConfigRefused, plan_codex_config  # noqa: E402
from tests.unit.toml_judge import JudgeFailure, judge_case  # noqa: E402

DATA_DIRS = (
    "/fuzz/vault",
    "/fuzz/vault#hash",
    "/fuzz/vault'quote",
    '/fuzz/vault"quote',
    "/fuzz/vault\\slash",
)
DATA_DIR = DATA_DIRS[0]
LABELS = (
    "install-shape",
    "producer-mcp-add",
    "producer-import",
    "env-before",
    "env-after",
    "tools-tables",
    "alice-absent",
    "comment-in-args",
    "basic-then-literal",
    "crlf-file",
    "crlf-in-string",
    "crlf-absent",
    "crlf-absent-no-final-newline",
    "bare-tools",
    "tools-key-and-table",
    "out-of-range",
)
MUTATIONS = (
    "comment-inside",
    "inline-alice",
    "bom",
    "drop-newline",
    "comment-in-args",
)


class FuzzFailure(AssertionError):
    """The writer changed a meaning the judge does not allow."""


@dataclass
class FuzzCounts:
    generated_written: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    refused_labels: dict[str, int] = field(default_factory=dict)
    by_mutation: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, mutation: str, outcome: str) -> None:
        per = self.by_mutation.setdefault(mutation, {})
        per[outcome] = per.get(outcome, 0) + 1


def _token(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(6))


def _comment(rng: random.Random, words: str = "kept") -> str:
    return f"# c{_token(rng)} {words}"


def _data_dir(rng: random.Random) -> str:
    return rng.choice(DATA_DIRS)


def _root(rng: random.Random) -> str:
    """Random root keys. Every base includes the forms a fixed template left out."""

    required = [
        "last_seen = 1979-05-27T07:32:00Z",
        'title = """before \'\'\' after"""',
        "ml4 = ''''four''''",
        "ml5 = '''''five'''''",
        'joined = """\\\nkept"""',
        'quoted = "say \\"\\"\\" hi"',
    ]
    optional = [
        "check_for_update_on_startup = false",
        "seen_on = 1979-05-27",
        "seen_at = 07:32:00",
        'note = "keep # hash"',
        'items = [\n["kept"],\n]',
        "flag = true",
        "count = 7",
    ]
    rng.shuffle(required)
    rng.shuffle(optional)
    chosen = required + optional[: rng.randint(2, len(optional))]
    rng.shuffle(chosen)
    lines = [_comment(rng, "root")]
    for piece in chosen:
        lines.append(piece)
        if rng.random() < 0.3:
            lines.append(_comment(rng, "between"))
    lines.append("")
    return "\n".join(lines)


def _install_shape(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
                "",
                _comment(rng, "after alice"),
                "[mcp_servers.other]",
                'command = "true"',
                "enabled = false",
                "",
            )
        )
    )


def _producer_mcp_add(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", \'C:\\Users\\Alex\\Alice Vault\']',
                "enabled = false",
                "startup_timeout_sec = 60.0",
                'default_tools_approval_mode = "approve"',
                "",
                "[mcp_servers.alice.env]",
                'ALICE_MCP_FULL_TOOLS = "1"',
                'ALICE_AGENT_API_KEY = \'a"b\'',
                "",
                "[mcp_servers.alice.tools.alice_recall]",
                'approval_mode = "approve"',
                "",
                _comment(rng, "later table"),
                '[projects."/tmp/proj"]',
                'trust_level = "trusted"',
                "",
            )
        )
    )


def _producer_import(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                "args = [",
                '    "alice-memory",',
                '    "mcp",',
                '    "--data-dir",',
                "    'C:\\Users\\Alex\\Alice Vault',",
                "]",
                'command = "uvx"',
                "env_vars = [",
                '    "HTTPS_PROXY",',
                '    "UV_INDEX_PRIVATE_USERNAME",',
                "]",
                "",
                "[mcp_servers.alice.env]",
                'ALICE_AGENT_API_KEY = """say "hi" and \'bye\'"""',
                "",
                _comment(rng, "after import"),
                "[mcp_servers.other]",
                'command = "true"',
                "enabled = false",
                "",
            )
        )
    )


def _env_table(rng: random.Random, *, before: bool) -> str:
    env = "\n".join(
        (
            "[mcp_servers.alice.env]",
            'ALICE_MEMORY_DATA_DIR = "/hand/vault"',
            'ALICE_MCP_FULL_TOOLS = "1"',
        )
    )
    main = "\n".join(
        (
            "[mcp_servers.alice]",
            'command = "uvx"',
            'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
        )
    )
    tools = "\n".join(
        (
            "[mcp_servers.alice.tools.alice_recall]",
            'approval_mode = "approve"',
        )
    )
    parts = [_root(rng).rstrip("\n")]
    if before:
        parts.extend((env, "", tools, "", main, ""))
    else:
        parts.extend((main, "", env, "", tools, ""))
    parts.append(_comment(rng, "tail"))
    return "\n".join(parts) + "\n"


def _tools(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice.tools.before]",
                'approval_mode = "prompt"',
                "",
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old#vault"]',
                "",
                _comment(rng, "between"),
                "[mcp_servers.alice.tools.after]",
                'approval_mode = "approve"',
                "",
            )
        )
    )


def _absent(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "banner = \"\"\"",
                "[mcp_servers.alice]",
                "\"\"\"",
                _comment(rng, "still outside"),
                '[projects."/tmp/proj"]',
                'trust_level = "trusted"',
                "",
            )
        )
    )


def _basic_then_literal(rng: random.Random) -> str:
    """A basic string holding ``'''`` sits directly before a one-line literal.

    Nothing else with ``'''`` comes between them. A lexer that treats those
    quotes as an opener then swallows the stamped comment.
    """

    return "\n".join(
        (
            'quoted = "x \'\'\' y"',
            "ticks = '''a # not a comment'''",
            _comment(rng, "after ticks"),
            "[mcp_servers.alice]",
            'command = "uvx"',
            'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
            "",
            _comment(rng, "after alice"),
        )
    )


def _crlf_file(rng: random.Random) -> str:
    return _install_shape(rng).replace("\n", "\r\n")


def _crlf_absent(rng: random.Random) -> str:
    """A CRLF file with no alice entry, so install appends a block."""

    return _absent(rng).replace("\n", "\r\n")


def _crlf_absent_no_final_newline(rng: random.Random) -> str:
    """The same, with the final line break removed."""

    text = _crlf_absent(rng)
    assert text.endswith("\r\n")
    return text[:-2]


def _crlf_in_string(rng: random.Random) -> str:
    return (
        "body = '''\r\n"
        "kept\r\n"
        "'''\n"
        + _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
                "",
                _comment(rng, "after crlf"),
            )
        )
    )


def _bare_tools(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
                "",
                "[mcp_servers.alice.tools] ",
                "[mcp_servers.alice.tools.alice_recall]",
                'approval_mode = "approve"',
                "",
                _comment(rng, "after tools"),
            )
        )
    )


def _tools_key_and_table(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
                'tools.alice_recall = { approval_mode = "approve" }',
                "",
                "[mcp_servers.alice.tools.after]",
                'approval_mode = "prompt"',
                "",
            )
        )
    )


def _out_of_range(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                'args = ["alice-memory", "mcp", "--data-dir", "/old/vault"]',
                "startup_timeout_sec = 9223372036854775808",
                "",
            )
        )
    )


def _comment_in_args(rng: random.Random) -> str:
    return (
        _root(rng)
        + "\n".join(
            (
                "[mcp_servers.alice]",
                'command = "uvx"',
                "args = [",
                '  "alice-memory", # pinned by hand, see ticket 42',
                '  "mcp",',
                '  "--data-dir",',
                '  "/old",',
                "]",
                "",
                _comment(rng, "outside args"),
            )
        )
    )


def generate(rng: random.Random, label: str) -> str:
    if label == "install-shape":
        return _install_shape(rng)
    if label == "producer-mcp-add":
        return _producer_mcp_add(rng)
    if label == "producer-import":
        return _producer_import(rng)
    if label == "env-before":
        return _env_table(rng, before=True)
    if label == "env-after":
        return _env_table(rng, before=False)
    if label == "tools-tables":
        return _tools(rng)
    if label == "comment-in-args":
        return _comment_in_args(rng)
    if label == "basic-then-literal":
        return _basic_then_literal(rng)
    if label == "crlf-file":
        return _crlf_file(rng)
    if label == "crlf-in-string":
        return _crlf_in_string(rng)
    if label == "crlf-absent":
        return _crlf_absent(rng)
    if label == "crlf-absent-no-final-newline":
        return _crlf_absent_no_final_newline(rng)
    if label == "bare-tools":
        return _bare_tools(rng)
    if label == "tools-key-and-table":
        return _tools_key_and_table(rng)
    if label == "out-of-range":
        return _out_of_range(rng)
    return _absent(rng)


def mutate(rng: random.Random, text: str) -> tuple[str, str]:
    kind = rng.choice(MUTATIONS)
    if kind == "comment-inside":
        return text.replace("[mcp_servers.alice]\n", "[mcp_servers.alice]\n# c" + _token(rng) + " inside\n", 1), kind
    if kind == "inline-alice":
        return 'mcp_servers = { alice = { command = "uvx" } }\n' + text, kind
    if kind == "bom":
        return "\ufeff" + text, kind
    if kind == "comment-in-args":
        needle = 'args = ["alice-memory"'
        if needle in text:
            return (
                text.replace(
                    needle,
                    'args = [\n  "alice-memory", # c' + _token(rng) + " inside args\n ",
                    1,
                ),
                kind,
            )
        return (
            text.replace(
                "[mcp_servers.alice]\n",
                '[mcp_servers.alice]\nargs = [\n  "alice-memory", # c' + _token(rng) + " inside args\n]\n",
                1,
            ),
            kind,
        )
    if text.endswith("\n"):
        return text[:-1], kind
    return text + "\r", kind


def check(text: str, *, label: str, mutant: bool, data_dir: str = DATA_DIR) -> str:
    refused = False
    output: str | None
    try:
        output = plan_codex_config(text, data_dir)
    except CodexConfigRefused:
        refused = True
        output = None
    try:
        judge_case(original=text, output=output, refused=refused, label=label, mutant=mutant)
    except JudgeFailure as exc:
        raise FuzzFailure(f"{label} mutant={mutant}: {exc}\n--- original\n{text}") from exc
    return "refused" if refused else "written"


def run(
    seeds: range, *, configs_per_seed: int = len(LABELS), mutants_per_config: int = 2
) -> FuzzCounts:
    counts = FuzzCounts()
    for seed in seeds:
        rng = random.Random(seed)
        for index in range(configs_per_seed):
            label = LABELS[index % len(LABELS)]
            text = generate(rng, label)
            data_dir = _data_dir(rng)
            outcome = check(text, label=label, mutant=False, data_dir=data_dir)
            if outcome == "written":
                counts.generated_written += 1
                counts.by_label[label] = counts.by_label.get(label, 0) + 1
            else:
                counts.refused_labels[label] = counts.refused_labels.get(label, 0) + 1
            for _ in range(mutants_per_config):
                mutant, kind = mutate(rng, text)
                counts.record(kind, check(mutant, label=label, mutant=True, data_dir=data_dir))
    return counts


def codex_bases(count: int, *, seed: int = 0) -> list[tuple[str, str]]:
    """Written configs and the data dir the plan used. Used by the real-host test."""

    rng = random.Random(seed)
    found: list[tuple[str, str]] = []
    index = 0
    while len(found) < count:
        label = LABELS[index % len(LABELS)]
        index += 1
        if label == "comment-in-args":
            continue
        text = generate(rng, label)
        data_dir = _data_dir(rng)
        try:
            written = plan_codex_config(text, data_dir)
        except CodexConfigRefused:
            continue
        found.append((text if written is None else written, data_dir))
        if index > count * 20:
            break
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        counts = run(range(args.seeds))
    except FuzzFailure as failure:
        print(f"FAIL: {failure}")
        return 1
    print(f"generated configs written: {counts.generated_written}")
    print(f"labels: {counts.by_label}")
    print(f"refused labels: {counts.refused_labels}")
    print(f"mutations: {counts.by_mutation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
