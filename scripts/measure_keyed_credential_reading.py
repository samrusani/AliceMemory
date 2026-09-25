"""Door-level before and after for keyed credential reading.

Each item is one note. The commit door receives canonical_text as a string.
Import receives a memory record. Before reads the import value column by
value only, which is what main did. After reads that column with its keys,
which is the import door now.

Counts are notes refused, not lines. Repo markdown is one note per file and
is string text, so keyed reading does not move that count. The synthetic
notes are the before and after. Credential values are built at runtime.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from alicebot_api.credential_floor import carries_credential_material, credential_verdict, string_values
from alicebot_api.legacy_credential_check import commit_door_secret_verdict
from alicebot_api.onramp import _memory_record_credential_fields, _memory_record_credential_finding


def _opaque() -> str:
    return "Xq9mZt2L" + "xP9wKc4BVq7m"


def _rollup_value() -> str:
    # The writer stores a 16-hex scope digest, then :topic: and the anchor.
    return "scope:" + hashlib.sha256(b"games").hexdigest()[:16] + ":topic:games"


def _record(
    memory_id: str,
    text: str,
    value: dict[str, object],
    metadata: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "id": memory_id,
        "title": text[:80],
        "canonical_text": text,
        "summary": text[:80],
        "value": value,
        "metadata_json": {} if metadata is None else metadata,
        "memory_key": "memory." + memory_id,
        "project_id": "alpha",
        "status": "active",
    }


def synthetic_notes() -> list[dict[str, object]]:
    opaque = _opaque()
    rollup = _rollup_value()
    digest = hashlib.sha256(b"workspace").hexdigest()
    routing = "agent:main:telegram:dm:4471"
    gpg_id = "A1B2" + "C3D4" + "E5F6" + "7890"
    ordinary = "Deploys go out on Tuesdays."
    return [
        {
            "id": "ordinary",
            "commit_text": ordinary,
            "record": _record("ordinary", ordinary, {"text": ordinary}),
        },
        {
            "id": "api_key",
            "commit_text": json.dumps({"api_key": opaque}),
            "record": _record("api_key", "A billing note.", {"text": "A billing note.", "api_key": opaque}),
        },
        {
            "id": "stripeKey",
            "commit_text": "stripeKey=" + opaque,
            "record": _record("stripeKey", "A billing note.", {"text": "A billing note.", "stripeKey": opaque}),
        },
        {
            "id": "openaiKey",
            "commit_text": "openaiKey=" + opaque,
            "record": _record("openaiKey", "A billing note.", {"text": "A billing note.", "openaiKey": opaque}),
        },
        {
            "id": "rollupKey",
            "commit_text": "rollupKey=" + opaque,
            "record": _record("rollupKey", "A note.", {"text": "A note.", "rollupKey": opaque}),
        },
        {
            "id": "ROLLUP_KEY",
            "commit_text": "ROLLUP_KEY=" + opaque,
            "record": _record("ROLLUP_KEY", "A note.", {"text": "A note.", "ROLLUP_KEY": opaque}),
        },
        {
            "id": "rollup-key",
            "commit_text": "rollup-key=" + opaque,
            "record": _record("rollup-key", "A note.", {"text": "A note.", "rollup-key": opaque}),
        },
        {
            "id": "product rollup_key",
            "commit_text": "Played several games.",
            "record": _record(
                "product-rollup",
                "Played several games.",
                {"text": "Played several games.", "rollup": {"rollup_key": rollup, "group_kind": "topic"}},
                {"rollup_key": rollup},
            ),
        },
        {
            "id": "continuity body rollup_key",
            "commit_text": json.dumps({"decision_text": "Ship on Tuesdays.", "rollup_key": opaque}),
            "record": _record(
                "continuity-body",
                "Ship on Tuesdays.",
                {"text": "Ship on Tuesdays.", "decision_text": "Ship on Tuesdays.", "rollup_key": opaque},
            ),
        },
        {
            "id": "rollup_key text",
            "commit_text": "rollup_key=" + opaque,
            "record": _record("rollup-text", "A note.", {"text": "A note."}),
        },
        {
            "id": "dedupe",
            "commit_text": "Imported note.",
            "record": _record(
                "dedupe",
                "Imported note.",
                {"text": "Imported note.", "openclaw_dedupe_key": digest},
            ),
        },
        {
            "id": "session_key",
            "commit_text": "Prefers short replies.",
            "record": _record(
                "session",
                "Prefers short replies.",
                {"text": "Prefers short replies.", "session_key": routing},
            ),
        },
        {
            "id": "gpg_key",
            "commit_text": "Signing setup.",
            "record": _record("gpg", "Signing setup.", {"text": "Signing setup.", "gpg_key": gpg_id}),
        },
    ]


def commit_refused(text: str) -> bool:
    return commit_door_secret_verdict("Note", text) is not None


def import_refused(record: dict[str, object], *, keyed: bool) -> bool:
    if keyed:
        return _memory_record_credential_finding(record, line_no=1) is not None
    values: list[object] = []
    for name, value in _memory_record_credential_fields(record):
        if name == "value":
            values.append(string_values(value))
        else:
            values.append(value)
    return credential_verdict(*values) is not None


def count_notes(notes: list[dict[str, object]]) -> dict[str, list[str]]:
    commit_ids: list[str] = []
    before_ids: list[str] = []
    after_ids: list[str] = []
    for note in notes:
        note_id = str(note["id"])
        if commit_refused(str(note["commit_text"])):
            commit_ids.append(note_id)
        record = note["record"]
        assert isinstance(record, dict)
        if import_refused(record, keyed=False):
            before_ids.append(note_id)
        if import_refused(record, keyed=True):
            after_ids.append(note_id)
    return {"commit": commit_ids, "import_before": before_ids, "import_after": after_ids}


def markdown_commit_refusals() -> tuple[int, int]:
    """One note per tracked markdown file, through the commit door."""

    root = Path(__file__).resolve().parents[1]
    listed = subprocess.check_output(["git", "ls-files", "*.md"], cwd=root, text=True)
    paths = [root / line for line in listed.splitlines() if line]
    refused = 0
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if commit_refused(text):
            refused += 1
    return len(paths), refused


def main() -> None:
    notes = synthetic_notes()
    counts = count_notes(notes)
    print(f"synthetic notes: {len(notes)}")
    print(f"commit door notes refused: {len(counts['commit'])} ({', '.join(counts['commit'])})")
    print(
        "import notes refused, value column by value (before): "
        f"{len(counts['import_before'])} ({', '.join(counts['import_before'])})"
    )
    print(
        "import notes refused, value column keyed (after): "
        f"{len(counts['import_after'])} ({', '.join(counts['import_after'])})"
    )
    files, refused = markdown_commit_refusals()
    print(f"markdown files as notes: {files}")
    print(f"markdown commit door notes refused: {refused}")
    print("markdown notes are string text, so keyed reading does not move that count")
    opaque = _opaque()
    body = {"decision_text": "Ship on Tuesdays.", "rollup_key": opaque}
    before = credential_verdict(opaque) is not None
    after = carries_credential_material(body)
    print(
        "continuity body rollup_key over an opaque value, value only (before): "
        + ("refused" if before else "kept")
    )
    print(
        "continuity body rollup_key over an opaque value, keyed (after): "
        + ("refused" if after else "kept")
    )


if __name__ == "__main__":
    main()
