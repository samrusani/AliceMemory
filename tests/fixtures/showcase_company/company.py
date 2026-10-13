"""A seeded fictional company for the showcase bench.

Quenby Fieldworks is not a real firm, and the people in the notes are not real
people. The same seed writes the same files. Two sizes are exact: 1,000 notes
and 10,000 notes. The dates run from 1 October 2024 through 31 March 2026,
eighteen months.

Alice stores one of thirteen domains and rejects any other name. The HR folder
is the company's HR domain. Those notes are stored as domain ``legal`` and
sensitivity ``confidential``, which is the label that folder sets, because
there is no domain named hr. A support key cannot read them.

A pricing draft is for sales and leadership. It is stored as domain
``financial`` and sensitivity ``private``. A support key may read an internal
note, so a draft is not given the sensitivity internal: the permission task
requires that a support agent be refused a draft price. A published price
change stays internal and professional, so the other teams can read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import random

COMPANY_NAME = "Quenby Fieldworks"
DEFAULT_SEED = 20261013
SIZES = (1000, 10000)
START = date(2024, 10, 1)
END = date(2026, 3, 31)

# The sensitivity the HR folder sets. A support key cannot read it.
HR_FOLDER_SENSITIVITY = "confidential"
HR_FOLDER_DOMAIN = "legal"
DRAFT_SENSITIVITY = "private"
DRAFT_DOMAIN = "financial"
DRAFT_AUDIENCE = ("leadership", "sales")

DOMAINS = (
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
)
SENSITIVITIES = (
    "public",
    "internal",
    "private",
    "confidential",
    "highly_sensitive",
    "sacred",
    "regulated",
    "unknown",
)

# Strings the notes never contain. The abstention questions ask about them.
ABSENT = (
    "founding day picnic",
    "warehouse rent in ledger",
    "competitor named Ostler",
)

FILLER_ACCOUNTS = (
    "Plover Yard",
    "Sable Kiln",
    "Wren Culvert",
    "Harth Cisterns",
    "Pellwick Castings",
    "Mothlane Goods",
    "Quill Orchard",
    "Hask Ferry",
)
FILLER_PRODUCTS = ("Harbor latch", "Culvert gauge", "Field spool", "North hinge")
ORDINARY_KINDS = (
    ("sales", "account_note", "sales/accounts", "professional", "internal"),
    ("sales", "call_summary", "sales/calls", "professional", "internal"),
    ("support", "ticket", "support/tickets", "professional", "internal"),
    ("support", "resolution", "support/resolutions", "professional", "internal"),
    ("product", "meeting", "product/meetings", "project", "internal"),
    ("product", "requirement", "product/requirements", "project", "internal"),
    ("product", "decision", "product/decisions", "project", "internal"),
    ("sales", "pricing_change", "pricing/changes", "professional", "internal"),
)


@dataclass(frozen=True)
class Note:
    relative_path: str
    team: str
    on: str
    sensitivity: str
    domain: str
    kind: str
    title: str
    body: str
    audience: tuple[str, ...] = ()

    def markdown(self) -> str:
        audience = ""
        if self.audience:
            audience = "audience: " + ", ".join(self.audience) + "\n"
        return (
            "---\n"
            f"team: {self.team}\n"
            f"date: {self.on}\n"
            f"sensitivity: {self.sensitivity}\n"
            f"domain: {self.domain}\n"
            f"kind: {self.kind}\n"
            f"{audience}"
            "---\n"
            "\n"
            f"# {self.title}\n"
            "\n"
            f"{self.body.rstrip()}\n"
        )


@dataclass(frozen=True)
class Anchor:
    """A string that appears in exactly the named notes and in no other note."""

    text: str
    paths: tuple[str, ...]
    task: str


def _note(
    path: str,
    team: str,
    on: str,
    sensitivity: str,
    domain: str,
    kind: str,
    title: str,
    body: str,
    audience: tuple[str, ...] = (),
) -> Note:
    return Note(path, team, on, sensitivity, domain, kind, title, body, audience)


def _planted() -> tuple[Note, ...]:
    """The notes that carry the bench's planted facts. They do not depend on the seed."""

    return (
        _note(
            "sales/accounts/span-open.md",
            "sales",
            "2024-10-01",
            "internal",
            "professional",
            "account_note",
            "Ledger year opens",
            "Quenby Fieldworks opened this ledger year on 1 October 2024. "
            "This note only marks the first day of the file.",
        ),
        _note(
            "product/meetings/span-close.md",
            "product",
            "2026-03-31",
            "internal",
            "project",
            "meeting",
            "Ledger year closes",
            "Quenby Fieldworks closed this eighteen month file on 31 March 2026. "
            "This note only marks the last day of the file.",
        ),
        _note(
            "sales/accounts/larkspur-contract.md",
            "sales",
            "2025-02-11",
            "internal",
            "professional",
            "account_note",
            "Larkspur Metals service contract",
            "On 11 February 2025 Larkspur Metals signed service contract QF-C-2041 "
            "for the Keel sensor kit. The term is 14 months. No other note states this contract.",
        ),
        _note(
            "pricing/changes/keel-480.md",
            "sales",
            "2025-01-15",
            "internal",
            "professional",
            "pricing_change",
            "Keel sensor kit list price set",
            "On 15 January 2025 the list price of the Keel sensor kit was set at 480 ledger units. "
            "This is the first price decision.",
        ),
        _note(
            "pricing/changes/keel-640.md",
            "sales",
            "2025-06-02",
            "internal",
            "professional",
            "pricing_change",
            "Keel sensor kit list price raised",
            "On 2 June 2025 the list price of the Keel sensor kit was raised to 640 ledger units. "
            "This replaces the list price decision of 15 January 2025.",
        ),
        _note(
            "pricing/changes/keel-520.md",
            "sales",
            "2025-11-18",
            "internal",
            "professional",
            "pricing_change",
            "Keel sensor kit list price cut",
            "On 18 November 2025 the list price of the Keel sensor kit was cut to 520 ledger units. "
            "This replaces the list price decision of 2 June 2025. 520 ledger units is the current published price.",
        ),
        _note(
            "pricing/drafts/keel-910.md",
            "sales",
            "2026-02-02",
            DRAFT_SENSITIVITY,
            DRAFT_DOMAIN,
            "pricing_draft",
            "Keel sensor kit draft price",
            "Draft list price 910 ledger for the Keel sensor kit. "
            "This draft is not published. It is for sales and leadership only.",
            DRAFT_AUDIENCE,
        ),
        _note(
            "sales/accounts/bramble-prospect.md",
            "sales",
            "2024-11-04",
            "internal",
            "professional",
            "account_note",
            "Bramble Dock prospect",
            "On 4 November 2024 Bramble Dock status prospect. No order is signed.",
        ),
        _note(
            "sales/accounts/bramble-active.md",
            "sales",
            "2025-03-20",
            "internal",
            "professional",
            "account_note",
            "Bramble Dock active",
            "On 20 March 2025 Bramble Dock status active. The first order shipped.",
        ),
        _note(
            "sales/calls/bramble-yard.md",
            "sales",
            "2025-08-01",
            "internal",
            "professional",
            "call_summary",
            "Bramble Dock call",
            "On 1 August 2025 a sales call recorded that Bramble Dock is adding a second yard.",
        ),
        _note(
            "support/resolutions/bramble-closed.md",
            "support",
            "2025-12-09",
            "internal",
            "professional",
            "resolution",
            "Bramble Dock closed",
            "On 9 December 2025 Bramble Dock status closed. "
            "Bramble Dock is not adding a yard. This is newer than the August call.",
        ),
        _note(
            "support/tickets/qf-441.md",
            "support",
            "2025-05-12",
            "internal",
            "professional",
            "ticket",
            "Cold start ticket",
            "Ticket QF-441 reports cold-start failures of the Keel sensor kit below four degrees. "
            "This ticket does not record a product decision.",
        ),
        _note(
            "product/decisions/operating-floor.md",
            "product",
            "2025-05-20",
            "internal",
            "project",
            "decision",
            "Operating floor",
            "On 20 May 2025 the operating floor moved to four degrees because of ticket QF-441. "
            "This note does not describe the failure the ticket reported.",
        ),
        _note(
            "product/decisions/harbor-mode-adopt.md",
            "product",
            "2025-07-01",
            "internal",
            "project",
            "decision",
            "Harbor mode adopted",
            "On 1 July 2025 product adopted harbor mode for the Culvert gauge.",
        ),
        _note(
            "product/decisions/harbor-mode-cancel.md",
            "product",
            "2025-10-14",
            "internal",
            "project",
            "decision",
            "Harbor mode cancelled",
            "On 14 October 2025 product cancelled harbor mode for the Culvert gauge. "
            "This replaces the harbor mode decision of 1 July 2025.",
        ),
        _note(
            "support/tickets/qf-778.md",
            "support",
            "2025-04-04",
            "internal",
            "professional",
            "ticket",
            "Seal leak observation",
            "Ticket QF-778 is the one observation of a seal leak on unit 17 at Plover Yard. "
            "No other visit reports that unit.",
        ),
        _note(
            "support/resolutions/qf-778-summary.md",
            "support",
            "2025-04-11",
            "internal",
            "professional",
            "resolution",
            "Summary of ticket QF-778",
            "This is a summary of ticket QF-778. It repeats the seal leak on unit 17 and adds no new visit.",
        ),
        _note(
            "sales/calls/qf-778-summary.md",
            "sales",
            "2025-04-18",
            "internal",
            "professional",
            "call_summary",
            "Sales summary of ticket QF-778",
            "Sales wrote a summary of ticket QF-778. It repeats the seal leak on unit 17 and adds no new visit.",
        ),
        _note(
            "product/meetings/qf-778-summary.md",
            "product",
            "2025-04-25",
            "internal",
            "project",
            "meeting",
            "Product summary of ticket QF-778",
            "Product minuted a summary of ticket QF-778. It repeats the seal leak on unit 17 and adds no new visit.",
        ),
        _note(
            "support/tickets/seal-unit-3.md",
            "support",
            "2025-06-19",
            "internal",
            "professional",
            "ticket",
            "Seal leak on another unit",
            "A separate visit recorded a seal leak on unit 3 at Sable Kiln. It is not ticket QF-778 and not unit 17.",
        ),
        _note(
            "support/tickets/seal-unit-9.md",
            "support",
            "2025-08-22",
            "internal",
            "professional",
            "ticket",
            "Seal leak on a third unit",
            "A separate visit recorded a seal leak on unit 9 at Wren Culvert. It is not ticket QF-778 and not unit 17.",
        ),
        _note(
            "hr/notes/mara-quen-pay.md",
            "hr",
            "2025-04-02",
            HR_FOLDER_SENSITIVITY,
            HR_FOLDER_DOMAIN,
            "hr_note",
            "Pay record",
            "Mara Quen compensation is 128400 ledger units for the year starting April 2025. "
            "This pay record is an HR note.",
        ),
    )


def anchors() -> tuple[Anchor, ...]:
    """Where each planted string is allowed to appear."""

    return (
        Anchor("service contract QF-C-2041", ("sales/accounts/larkspur-contract.md",), "recall"),
        Anchor("set at 480 ledger units", ("pricing/changes/keel-480.md",), "change"),
        Anchor("raised to 640 ledger units", ("pricing/changes/keel-640.md",), "change"),
        Anchor("cut to 520 ledger units", ("pricing/changes/keel-520.md",), "change"),
        Anchor("Draft list price 910 ledger", ("pricing/drafts/keel-910.md",), "permission"),
        Anchor("Bramble Dock status prospect", ("sales/accounts/bramble-prospect.md",), "change"),
        Anchor("Bramble Dock status active", ("sales/accounts/bramble-active.md",), "change"),
        Anchor("Bramble Dock status closed", ("support/resolutions/bramble-closed.md",), "change"),
        Anchor("adding a second yard", ("sales/calls/bramble-yard.md",), "contradiction"),
        Anchor("not adding a yard", ("support/resolutions/bramble-closed.md",), "contradiction"),
        Anchor(
            "Ticket QF-441 reports cold-start failures",
            ("support/tickets/qf-441.md",),
            "connection",
        ),
        Anchor(
            "operating floor moved to four degrees because of ticket QF-441",
            ("product/decisions/operating-floor.md",),
            "connection",
        ),
        Anchor("adopted harbor mode", ("product/decisions/harbor-mode-adopt.md",), "change"),
        Anchor("cancelled harbor mode", ("product/decisions/harbor-mode-cancel.md",), "change"),
        Anchor(
            "one observation of a seal leak on unit 17",
            ("support/tickets/qf-778.md",),
            "corroboration",
        ),
        Anchor(
            "summary of ticket QF-778",
            (
                "support/resolutions/qf-778-summary.md",
                "sales/calls/qf-778-summary.md",
                "product/meetings/qf-778-summary.md",
            ),
            "corroboration",
        ),
        Anchor("seal leak on unit 3", ("support/tickets/seal-unit-3.md",), "corroboration"),
        Anchor("seal leak on unit 9", ("support/tickets/seal-unit-9.md",), "corroboration"),
        Anchor("compensation is 128400 ledger units", ("hr/notes/mara-quen-pay.md",), "permission"),
    )


def _quotas(size: int) -> dict[str, int]:
    if size == 1000:
        return {"hr": 40, "pricing_draft": 20}
    if size == 10000:
        return {"hr": 200, "pricing_draft": 80}
    raise ValueError("size must be 1000 or 10000")


def _filler_note(index: int, on: str, seed: int, kind_row: tuple[str, str, str, str, str]) -> Note:
    team, kind, folder, domain, sensitivity = kind_row
    account = FILLER_ACCOUNTS[index % len(FILLER_ACCOUNTS)]
    product = FILLER_PRODUCTS[index % len(FILLER_PRODUCTS)]
    count = 2 + (index % 17)
    path = f"{folder}/filler-{index:05d}.md"
    title = f"{kind.replace('_', ' ')} {index}"
    body = (
        f"Note {index} of seed {seed} for {account} on {on}. "
        f"The {product} shipment count is {count}. "
        "This is an ordinary file note and states no planted bench fact."
    )
    return _note(path, team, on, sensitivity, domain, kind, title, body)


def _special_filler(index: int, on: str, seed: int, which: str) -> Note:
    if which == "hr":
        path = f"hr/notes/filler-{index:05d}.md"
        return _note(
            path,
            "hr",
            on,
            HR_FOLDER_SENSITIVITY,
            HR_FOLDER_DOMAIN,
            "hr_note",
            f"HR file note {index}",
            f"HR file note {index} of seed {seed} on {on}. It records a routine staffing count of {2 + index % 9} "
            "and no pay figure.",
            ("leadership",),
        )
    path = f"pricing/drafts/filler-{index:05d}.md"
    return _note(
        path,
        "sales",
        on,
        DRAFT_SENSITIVITY,
        DRAFT_DOMAIN,
        "pricing_draft",
        f"Pricing draft {index}",
        f"Pricing draft {index} of seed {seed} on {on} for the Field spool. "
        "The draft figure is 300 ledger units. It is not published.",
        DRAFT_AUDIENCE,
    )


def _dates(count: int, rng: random.Random) -> list[str]:
    span = (END - START).days
    picked = [START + timedelta(days=rng.randrange(span + 1)) for _ in range(count)]
    return [item.isoformat() for item in picked]


def generate(size: int, *, seed: int = DEFAULT_SEED) -> tuple[Note, ...]:
    """The notes for one size. ``size`` is 1000 or 10000. The same seed repeats."""

    if size not in SIZES:
        raise ValueError("size must be 1000 or 10000")
    planted = list(_planted())
    quotas = _quotas(size)
    hr_have = sum(1 for note in planted if note.kind == "hr_note")
    draft_have = sum(1 for note in planted if note.kind == "pricing_draft")
    hr_more = quotas["hr"] - hr_have
    draft_more = quotas["pricing_draft"] - draft_have
    if hr_more < 0 or draft_more < 0:
        raise RuntimeError("planted notes already pass the folder quota")
    ordinary = size - len(planted) - hr_more - draft_more
    if ordinary < 0:
        raise RuntimeError("size is smaller than the planted notes")
    rng = random.Random(seed)
    dates = _dates(hr_more + draft_more + ordinary, rng)
    cursor = 0
    extra: list[Note] = []
    for offset in range(hr_more):
        extra.append(_special_filler(10_000 + offset, dates[cursor], seed, "hr"))
        cursor += 1
    for offset in range(draft_more):
        extra.append(_special_filler(20_000 + offset, dates[cursor], seed, "draft"))
        cursor += 1
    for offset in range(ordinary):
        kind_row = ORDINARY_KINDS[offset % len(ORDINARY_KINDS)]
        extra.append(_filler_note(30_000 + offset, dates[cursor], seed, kind_row))
        cursor += 1
    notes = tuple(sorted([*planted, *extra], key=lambda note: note.relative_path))
    if len(notes) != size:
        raise RuntimeError(f"generated {len(notes)} notes, not {size}")
    problems = validate(notes)
    if problems:
        raise RuntimeError(problems[0])
    return notes


def validate(notes: tuple[Note, ...] | list[Note]) -> list[str]:
    """Problems in a generated company. An empty list means the company holds."""

    problems: list[str] = []
    paths = [note.relative_path for note in notes]
    if len(paths) != len(set(paths)):
        problems.append("two notes share a path")
    by_path = {note.relative_path: note for note in notes}
    texts = {note.relative_path: note.markdown() for note in notes}
    dates = []
    for note in notes:
        if note.team not in {"sales", "support", "product", "leadership", "hr"}:
            problems.append(f"{note.relative_path} has team {note.team}")
        if note.domain not in DOMAINS or note.sensitivity not in SENSITIVITIES:
            problems.append(f"{note.relative_path} has a label Alice cannot store")
        if not note.on or not note.kind:
            problems.append(f"{note.relative_path} lacks a date or a kind")
        try:
            on = date.fromisoformat(note.on)
        except ValueError:
            problems.append(f"{note.relative_path} has a bad date")
            continue
        dates.append(on)
        if on < START or on > END:
            problems.append(f"{note.relative_path} is outside the eighteen months")
        if note.kind == "hr_note" and (note.sensitivity != HR_FOLDER_SENSITIVITY or note.domain != HR_FOLDER_DOMAIN):
            problems.append(f"{note.relative_path} is an HR note with the wrong label")
        if note.kind == "pricing_draft" and (
            note.sensitivity != DRAFT_SENSITIVITY or note.domain != DRAFT_DOMAIN or note.audience != DRAFT_AUDIENCE
        ):
            problems.append(f"{note.relative_path} is a pricing draft with the wrong label")
    if dates and (min(dates) != START or max(dates) != END):
        problems.append("the file does not open on the first day and close on the last day")
    for anchor in anchors():
        found = tuple(sorted(path for path, text in texts.items() if anchor.text in text))
        if found != tuple(sorted(anchor.paths)):
            problems.append(f"anchor {anchor.text!r} is in {found}, not {anchor.paths}")
        for path in anchor.paths:
            if path not in by_path:
                problems.append(f"anchor path {path} is missing")
    for absent in ABSENT:
        found = [path for path, text in texts.items() if absent in text]
        if found:
            problems.append(f"{absent!r} appears in {found[0]}")
    return problems


def write_notes(notes: tuple[Note, ...] | list[Note], directory: Path) -> None:
    """Write one Markdown file per note under ``directory``."""

    root = directory
    root.mkdir(parents=True, exist_ok=True)
    expected = {note.relative_path for note in notes}
    for note in notes:
        path = root / note.relative_path
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("a note path leaves the output directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note.markdown(), encoding="utf-8")
    for path in root.rglob("*.md"):
        relative = path.relative_to(root).as_posix()
        if relative not in expected:
            path.unlink()
