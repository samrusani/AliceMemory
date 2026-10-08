"""A store method that defers its lock decision to the clamp must call the clamp next."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
DEFERRING = re.compile(r"prepare_label_patch\([^\n]*clamp_follows=True\)")
PAIRED = re.compile(
    r'patch = prepare_label_patch\(self, "(?P<kind>\w+)", (?P<before>\w+), patch, clamp_follows=True\)\n'
    r'\s+patch = clamp_owner_patch\(self, kind="(?P=kind)", before=(?P=before), patch=patch\)'
)


def test_every_deferring_prepare_is_followed_by_its_clamp():
    deferring = paired = 0
    for path in SOURCE.rglob("*.py"):
        text = path.read_text()
        deferring += len(DEFERRING.findall(text))
        paired += len(PAIRED.findall(text))
    # Memory and open-loop updates on PostgreSQL, and the open-loop update on SQLite.
    assert deferring == 3
    assert paired == deferring


def test_only_memory_and_open_loop_updates_defer():
    kinds = {
        match.group("kind")
        for path in SOURCE.rglob("*.py")
        for match in PAIRED.finditer(path.read_text())
    }
    assert kinds == {"memory", "open_loop"}
