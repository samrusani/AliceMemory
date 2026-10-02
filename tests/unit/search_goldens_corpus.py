"""The invented Markdown folder the search goldens are recorded from.

Every document is made up. The folder is small on purpose and shaped to the
ways a search change moves an output, not to be realistic:

* ``runbook.md``, ``ledger-design.md`` and ``faq.md`` each hold several sections.
  The importer ends a chunk at a heading, so each section is its own chunk and a
  question that touches several sections finds several chunks of one document.
* ``runbook.md`` and ``faq.md`` end with the same "Support hours" section, so two
  sources hold one identical chunk of text.
* ``faq.md`` holds a contraction ("doesn't") and two version numbers, which the
  query builders split into fragments.
* ``changelog-draft.md`` names versions in its headings.
* ``handbook.md`` is a one-chunk document.
* ``vendor-shortlist.md`` is imported as ``confidential`` and ``garden.md`` as a
  ``personal`` and ``private`` note, so the default fences have something to hold back.

No text here may carry a dash character other than the ASCII hyphen, a path, a
person who exists or anything that looks like a credential.
"""

from __future__ import annotations

from pathlib import Path

SUPPORT_HOURS = (
    "## Support hours\n\n"
    "Support hours are weekdays from nine to five, Harbor Lantern time. Outside those hours the pager owner "
    "answers urgent issues only.\n"
)

RUNBOOK = (
    "# Harbor Lantern release runbook\n\n"
    "## The release gate\n\n"
    "The Harbor Lantern release gate has three checks: the unit battery, the packaging smoke test and the "
    "upgrade rehearsal. The gate must be green on the exact commit before anyone tags it. A green gate on a "
    "branch tip does not count for the merge commit.\n\n"
    "## Tagging the release\n\n"
    "Tag the release only after the gate is green and both approvals are recorded. Use an annotated tag on the "
    "merge commit. Never tag from a branch tip, because the tag then points at a commit the gate never saw.\n\n"
    "## Rolling back\n\n"
    "To roll back a bad release, republish the previous wheel under a new patch number and mark the bad one as "
    "yanked. Do not delete a published version. The rollback owner is Orla Vance.\n\n"
    "## Pager rota\n\n"
    "Orla Vance holds the pager on odd weeks and Tomas Reyes holds it on even weeks. The rota changes every "
    "Monday at nine in the morning.\n\n" + SUPPORT_HOURS
)

#: The runbook after one fact changed, for the import that edits a file in place.
RUNBOOK_EDITED = RUNBOOK.replace(
    "on odd weeks and Tomas Reyes holds it on even weeks",
    "on even weeks and Tomas Reyes holds it on odd weeks",
)

LEDGER_DESIGN = (
    "# Ledger design\n\n"
    "## Retry budget\n\n"
    "Ledger writers retry a failed append three times with exponential backoff and jitter. After the third "
    "failure the writer parks the entry in the retry queue and raises an alert.\n\n"
    "## Append only\n\n"
    "The ledger is append only. A correction is a new entry that points at the entry it replaces. Nothing is "
    "ever updated in place, and the nightly compaction job never rewrites an entry.\n\n"
    "## Migration to v2.4.1\n\n"
    "The v2.4.1 migration adds the settlement column. Run it before the nightly compaction. If the migration "
    "fails, the compaction job refuses to start and the pager owner is told.\n"
)

FAQ = (
    "# Harbor Lantern FAQ\n\n"
    "## Why does the doctor not print secrets?\n\n"
    "I'm not sure anyone would expect it to. The doctor doesn't print secrets because a diagnostic report is "
    "often pasted into a ticket. It prints the length of a flagged value and the word withheld instead.\n\n"
    "## Which version added the withheld marker?\n\n"
    "The withheld marker arrived in v0.19.2 and the length hint in v0.20.0. Older versions print nothing at "
    "all for a flagged value.\n\n"
    "## Who do I ask about the pager?\n\n"
    "Ask Orla Vance for the pager rota, or Tomas Reyes when she is away.\n\n" + SUPPORT_HOURS
)

CHANGELOG_DRAFT = (
    "# Changelog draft\n\n"
    "## v0.20.0\n\n"
    "The doctor prints the length of a flagged value. The ledger writer retries a failed append three times.\n\n"
    "## v0.19.2\n\n"
    "The doctor prints the word withheld for a flagged value. The nightly compaction job skips a locked "
    "partition and tries it again the next night.\n"
)

HANDBOOK = "# Harbor Lantern handbook\n\nThis folder holds the runbook, the ledger design and the FAQ.\n"

VENDOR_SHORTLIST = (
    "# Vendor shortlist\n\n"
    "## Shortlist\n\n"
    "The confidential vendor shortlist names Ember Quay as the preferred supplier of the settlement hardware.\n\n"
    "## Fallback\n\n"
    "If Ember Quay declines, the fallback supplier for the settlement hardware is Northgate Works.\n"
)

GARDEN = (
    "# Garden notes\n\n"
    "## Frost dates\n\n"
    "The tomato beds need covers before the first frost, usually in the second week of October.\n\n"
    "## Watering\n\n"
    "Water the beds early in the morning, never in the evening, and skip a day after rain.\n"
)

#: ``(folder, file name, text)`` for the main folder. The importer takes the files of a folder in path order, so the
#: order written here is not the order they are captured in.
MAIN_FOLDER_FILES: tuple[tuple[str, str, str], ...] = (
    ("harbor-lantern", "runbook.md", RUNBOOK),
    ("harbor-lantern", "ledger-design.md", LEDGER_DESIGN),
    ("harbor-lantern", "faq.md", FAQ),
    ("harbor-lantern", "changelog-draft.md", CHANGELOG_DRAFT),
    ("harbor-lantern", "handbook.md", HANDBOOK),
)

#: Imported on their own so each carries its own labels.
VENDOR_FILE = ("vendor", "vendor-shortlist.md", VENDOR_SHORTLIST)
GARDEN_FILE = ("home", "garden.md", GARDEN)


def write_folder(root: Path, files: tuple[tuple[str, str, str], ...]) -> None:
    """Write each ``(folder, name, text)`` under ``root``, creating folders."""

    for folder, name, text in files:
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(text, encoding="utf-8")
