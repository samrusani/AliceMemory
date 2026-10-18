"""The differential check of the what-changed reason, shared by the SQLite and the PostgreSQL tests.

Every citation shape of the legacy catalog and of the saved-quote catalogs is planted as the references of a decision, alone and
with an entry of plain words before it and after it. The reason that each step shows, at each door and for each caller, is
compared with what the other readers show the same caller of the same row:

* the first quote the saved-quote reader leaves in the row (``SavedProvenanceReader.memory``, the code the pack and
  ``alice_explain`` call) is the oracle for every row and every caller;
* the full context pack is the second oracle, for the rows of the layout that puts the words in a sibling entry and for the
  callers it answers with the reference list of a memory.

The backend (SQLite or PostgreSQL) supplies the few operations that differ: planting, reading at a door, asking the pack and
asking the reader.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from alicebot_api.recall_framing import quote_session_brief_text

LAYOUTS = ("alone", "words after", "words before")
# One token for the rows a what-changed read takes together (it scans 128 candidates), and one for each group of rows the pack
# is asked about (the layout that puts the words in a sibling entry, which is the shape the pack is compared on).
READ_BATCH = 120
PACK_BATCH = 20
# The caller of the review matrix that has no limits at all, and the owner, who has none either.
UNLIMITED = {"owner", "admin"}
# The callers the full context pack answers with the reference list of a memory. A caller bound to a project is given a pack
# whose scope pass drops the reference list, and a project-scoped key with no project is refused, so neither compares.
PACK_CALLERS = ("read_only", "memory_proposal", "trusted", "admin", "declared", "owner")
SIBLING = "SIBLINGQUOTE"


def entries(entry: object, layout: str) -> list[object]:
    if layout == "alone":
        return [entry]
    if layout == "words after":
        return [entry, {"quote": SIBLING}]
    return [{"quote": SIBLING}, entry]


def shown_quotes(row: Mapping[str, object]) -> list[str]:
    """The quote of every mapping entry in the refs of ``row``, in the order the read looks for a reason."""

    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    agentic = metadata.get("agentic_memory")
    agentic = agentic if isinstance(agentic, Mapping) else {}
    found: list[str] = []
    for container, keys in ((metadata, ("source_refs", "provenance", "replacement_provenance")), (agentic, ("source_refs",))):
        for key in keys:
            value = container.get(key)
            listed = value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
            for entry in listed:
                if isinstance(entry, Mapping) and isinstance(entry.get("quote"), str) and entry["quote"].strip():
                    found.append(entry["quote"].strip())
    return found


def first(quotes: list[str]) -> str | None:
    return quote_session_brief_text(quotes[0]) if quotes else None


def reasons_of(answer: Mapping[str, object]) -> dict[str, object]:
    """The reason of every step of every chain, keyed by the step id."""

    found: dict[str, object] = {}
    for chain in answer["chains"]:  # type: ignore[union-attr]
        for step in chain["history"]:
            found[str(step["id"])] = step["reason"]
    return found


@dataclass(frozen=True)
class Planted:
    memory_id: str
    name: str
    read_token: str
    pack_token: str


class Backend(Protocol):
    """What differs between the two databases."""

    def targets(self) -> tuple[str, str, str, str]:
        """Ids of a public source, a confidential source, a confidential memory and a public memory."""

    def plant_many(self, decisions: list[tuple[str, str, list[object]]]) -> list[str]:
        """Store each (title, text, refs) as a decision whose commit saved ``refs``, and return the ids in order."""

    def make_keys(self) -> dict[str, str]:
        """One key of each profile of the matrix, named as ``KEYED`` names them."""

    def read(self, key: str | None, token: str, *, declared: bool) -> dict[str, dict[str, object]]:
        """The reasons the doors give for a topic: ``route`` (not for a declared identity) and ``tool``."""

    def pack(self, key: str | None, token: str, *, declared: bool) -> dict[str, Mapping[str, object]]:
        """The memory rows of the full context pack, keyed by id."""

    def reader_rows(self, key: str | None, ids: list[str], *, declared: bool) -> list[Mapping[str, object]]:
        """The rows as the saved-quote reader leaves them for this caller, in the order of ``ids``."""


def run_quote_differential(
    backend: Backend,
    cases: Callable[[str, str, str, str], list[tuple[str, object]]],
) -> dict[str, tuple[int, int]]:
    """Plant every case in every layout and compare each caller's reasons with the oracles. Returns, for each caller, the
    number of rows whose reason is shown and the number whose reason is null."""

    public_id, hidden_id, secret_id, open_id = backend.targets()
    specs: list[tuple[str, str, str, str, list[object]]] = []
    packed_count = 0
    for name, entry in cases(public_id, hidden_id, secret_id, open_id):
        for layout in LAYOUTS:
            read_token = f"zxqdiff{len(specs) // READ_BATCH}"
            pack_token = ""
            if layout == "words after":
                pack_token = f"zxqpack{packed_count // PACK_BATCH}"
                packed_count += 1
            specs.append((f"{name}/{layout}", read_token, pack_token, f"{read_token} {pack_token} keel decision {len(specs)}",
                          entries(entry, layout)))
    ids = backend.plant_many([(title, f"{read} recorded decision", refs) for _name, read, _pack, title, refs in specs])
    planted = [Planted(memory_id, name, read, pack) for memory_id, (name, read, pack, _title, _refs) in zip(ids, specs, strict=True)]
    read_tokens = sorted({row.read_token for row in planted})
    pack_tokens = sorted({row.pack_token for row in planted if row.pack_token})
    assert len(planted) > 1500 and len(read_tokens) >= 12 and len(pack_tokens) > 25

    def read(key: str | None, *, declared: bool = False) -> dict[str, dict[str, object]]:
        merged: dict[str, dict[str, object]] = {}
        for token in read_tokens:
            for door, answer in backend.read(key, token, declared=declared).items():
                merged.setdefault(door, {}).update(reasons_of(answer))
        return merged

    def pack(key: str | None, *, declared: bool = False) -> dict[str, Mapping[str, object]]:
        found: dict[str, Mapping[str, object]] = {}
        for token in pack_tokens:
            rows = backend.pack(key, token, declared=declared)
            wanted = {row.memory_id for row in planted if row.pack_token == token}
            assert set(rows) == wanted, (token, len(rows), len(wanted))
            found.update(rows)
        return found

    problems: list[tuple[str, str, str, object, object]] = []

    def judge(
        who: str,
        key: str | None,
        answers: dict[str, dict[str, object]],
        packed: dict[str, Mapping[str, object]] | None,
        *,
        declared: bool = False,
    ) -> tuple[int, int]:
        shown_rows = backend.reader_rows(key, [row.memory_id for row in planted], declared=declared)
        shown = kept = 0
        held_back: list[str] = []
        for row, left in zip(planted, shown_rows, strict=True):
            reader_quotes = shown_quotes(left)
            expected = first(reader_quotes)
            for door, reasons in answers.items():
                reason = reasons[row.memory_id]
                # A reason may name the source its quote came from, which the entry decides. The text is the oracle.
                got = reason["text"] if isinstance(reason, dict) else None
                if got != expected:
                    problems.append((who, door, row.name, expected, got))
            if packed is not None and row.pack_token:
                pack_quotes = shown_quotes(packed[row.memory_id])
                if who in UNLIMITED:
                    # The pack goes on after the reader and drops an id of a memory outside the filter of its request, also
                    # for a caller with no limits, so it may hold back a quote that is a bare reference to one.
                    if not set(pack_quotes) <= set(reader_quotes):
                        problems.append((who, "pack", row.name, reader_quotes, pack_quotes))
                    if first(pack_quotes) != expected:
                        held_back.append(row.name)
                elif first(pack_quotes) != expected:
                    problems.append((who, "pack", row.name, expected, first(pack_quotes)))
            shown += expected is not None
            kept += expected is None
        assert all(name.startswith("typed-memory-typed-object-quote/") for name in held_back), (who, held_back)
        return shown, kept

    results: dict[str, tuple[int, int]] = {}
    # The owner and the declared identity are read before any key exists: the route refuses a call with no key afterwards.
    results["owner"] = judge("owner", None, read(None), pack(None))
    results["declared"] = judge("declared", None, read(None, declared=True), pack(None, declared=True), declared=True)
    keys = backend.make_keys()
    for name, key in keys.items():
        results[name] = judge(name, key, read(key), pack(key) if name in PACK_CALLERS else None)
    # Every caller and every row is judged before the check fails, so a failure names each caller that differs and some of its rows.
    by_caller: dict[str, list[tuple[str, str, str, object, object]]] = {}
    for problem in problems:
        by_caller.setdefault(problem[0], []).append(problem)
    assert not problems, {who: (len(rows), rows[:3]) for who, rows in by_caller.items()}
    return results
