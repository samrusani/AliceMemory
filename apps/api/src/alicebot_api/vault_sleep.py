"""Offline sleep pass for ``alice-memory sleep``.

Writes a capped sidecar of source proposals. Import stays a source.
Commit stays a fact. Search is unchanged. Accept is a later commit.

Sleep proposes the oldest sources first (``captured_at``, then id). The
session brief shows the newest. A row stops counting toward the cap once
its source has an active or accepted memory. The row stays in the file.
Accepting a proposal does not edit the sidecar.

The commit door's credential check runs on the excerpt and on the cut
window of the first chunk. A refusal is not written. An existing refused
row is removed. The cap counts those kept rows, except a row whose
source already has an active or accepted memory. The listing runs the
same check again. It prints that source's domain, sensitivity, and
project scope on the commit line, and it does not offer a source that
already has an active or accepted memory.

Does not create memories, does not rewrite sources or committed facts,
and does not call consolidation. Counts and sidecar rows bind ``user_id``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from alicebot_api.legacy_credential_check import commit_door_secret_verdict
from alicebot_api.session_briefing import (
    COMMITTED_MEMORY_STATUSES,
    SESSION_BRIEF_FRAME,
    _source_honours_fence,
    quote_session_brief_text,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_project_scope import source_project_scope

SLEEP_PROPOSAL_CAP = 8
SLEEP_PROPOSAL_FILENAME = "sleep_proposals.jsonl"
SLEEP_EXCERPT_MAX = 160
PROPOSED_STATUS = "proposed"
NO_SLEEP_PROPOSALS = "No sleep proposals."
CAP_FULL_WAIT = (
    "sleep adds no proposals for this user until a counting row is removed "
    "from the sidecar or its source has an active or accepted memory"
)

LIST_SOURCE_IDS_SQL = """
SELECT id
FROM sources
WHERE user_id = ?
  AND deleted_at IS NULL
ORDER BY captured_at ASC, id ASC
"""


class SleepError(ValueError):
    """A user-facing sleep failure with a static CLI error code."""


def sleep_proposals_path(db_path: Path) -> Path:
    """Sidecar JSONL next to the SQLite file."""

    return Path(db_path).expanduser().resolve().parent / SLEEP_PROPOSAL_FILENAME


def format_sleep_receipt(
    *,
    written: int,
    already_present: int,
    skipped_linked: int,
    cap: int,
    sources_withheld: int,
    existing_rows_removed: int,
    sources_not_proposed: int = 0,
    sidecar: Path | None = None,
) -> str:
    """Receipt counts only. No excerpts, values, or source ids."""

    lines = [
        f"proposals written: {written}",
        f"already present: {already_present}",
        f"skipped as already linked: {skipped_linked}",
        f"cap: {cap}",
        f"sources withheld: {sources_withheld}",
        f"existing rows removed: {existing_rows_removed}",
    ]
    if sources_not_proposed > 0:
        lines.append(f"sources not proposed: {sources_not_proposed}")
        lines.append(f"{CAP_FULL_WAIT}: {sidecar}")
    return "\n".join(lines)


def load_sleep_proposals(path: Path) -> list[dict[str, object]]:
    """Read existing sidecar rows. Fail closed on a corrupt file."""

    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SleepError("sidecar could not be read") from exc
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SleepError("sidecar is invalid") from exc
        if not isinstance(parsed, dict):
            raise SleepError("sidecar is invalid")
        if "user_id" not in parsed or "source_id" not in parsed:
            raise SleepError("sidecar is invalid")
        rows.append(parsed)
    return rows


def run_local_vault_sleep(
    db_path: Path,
    *,
    user_id: UUID | str,
) -> str:
    """Propose unlinked imported sources into the sidecar. Cap, then stop."""

    if COMMITTED_MEMORY_STATUSES != ("active", "accepted"):
        raise RuntimeError(
            "sleep committed-fact filter is written for active and accepted only"
        )

    resolved = Path(db_path).expanduser().resolve()
    sidecar = sleep_proposals_path(resolved)
    existing = load_sleep_proposals(sidecar)

    with sqlite_user_connection(resolved, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        uid = store.user_id
        kept_rows: list[dict[str, object]] = []
        existing_rows_removed = 0
        for row in existing:
            if _row_refused(store, row, caller_id=uid):
                if str(row.get("user_id") or "") == uid:
                    existing_rows_removed += 1
                continue
            kept_rows.append(row)
        existing_ids: set[str] = set()
        counting_ids: set[str] = set()
        for row in kept_rows:
            if str(row.get("user_id") or "") != uid:
                continue
            source_id = row.get("source_id")
            if not isinstance(source_id, str) or source_id == "":
                continue
            existing_ids.add(source_id)
            if _has_committed_fact(store, source_id):
                continue
            counting_ids.add(source_id)
        written_rows: list[dict[str, object]] = []
        already_present = 0
        skipped_linked = 0
        sources_not_proposed = 0
        sources_withheld = 0
        for source_id in _list_source_ids(store):
            if _has_committed_fact(store, source_id):
                skipped_linked += 1
                continue
            if source_id in existing_ids:
                already_present += 1
                continue
            chunk = _first_chunk_text(store, source_id)
            excerpt = _short_excerpt(chunk) if chunk else ""
            checked = _credential_window(chunk) if chunk else ""
            if _text_refused(excerpt) or _text_refused(checked):
                sources_withheld += 1
                continue
            if len(counting_ids) + len(written_rows) >= SLEEP_PROPOSAL_CAP:
                sources_not_proposed += 1
                continue
            written_rows.append(
                {
                    "excerpt": excerpt,
                    "source_id": source_id,
                    "status": PROPOSED_STATUS,
                    "user_id": uid,
                }
            )

    dropped = len(existing) - len(kept_rows)
    if written_rows or dropped:
        try:
            _write_jsonl(sidecar, [*kept_rows, *written_rows])
        except OSError as exc:
            raise SleepError("sidecar could not be written") from exc
    else:
        try:
            _restrict_sidecar_mode(sidecar)
        except OSError as exc:
            raise SleepError("sidecar could not be written") from exc

    return format_sleep_receipt(
        written=len(written_rows),
        already_present=already_present,
        skipped_linked=skipped_linked,
        cap=SLEEP_PROPOSAL_CAP,
        sources_withheld=sources_withheld,
        existing_rows_removed=existing_rows_removed,
        sources_not_proposed=sources_not_proposed,
        sidecar=sidecar,
    )


def _list_source_ids(store: SQLiteVNextStore) -> list[str]:
    rows = store.conn.execute(LIST_SOURCE_IDS_SQL, (store.user_id,)).fetchall()
    ids: list[str] = []
    for row in rows:
        value = row["id"] if isinstance(row, dict) else row[0]
        ids.append(str(value))
    return ids


def _has_committed_fact(store: SQLiteVNextStore, source_id: str) -> bool:
    linked = store.list_memories_referencing_source(source_id=source_id)
    return any(str(row.get("status") or "") in COMMITTED_MEMORY_STATUSES for row in linked)


def _first_chunk_text(store: SQLiteVNextStore, source_id: str) -> str:
    for chunk in store.list_source_chunks(source_id):
        text = chunk.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _text_refused(text: object) -> bool:
    """True when the commit door would refuse this one string."""

    if not isinstance(text, str) or text == "":
        return False
    return commit_door_secret_verdict("", text) is not None


def _row_refused(store: SQLiteVNextStore, row: dict[str, object], *, caller_id: str) -> bool:
    """Caller rows: stored excerpt and the source window. Other users: excerpt."""

    if _text_refused(row.get("excerpt")):
        return True
    if str(row.get("user_id") or "") != caller_id:
        return False
    source_id = row.get("source_id")
    if not isinstance(source_id, str) or source_id == "":
        return False
    if store.get_source(source_id) is None:
        return False
    chunk = _first_chunk_text(store, source_id)
    if not chunk:
        return False
    return _text_refused(_short_excerpt(chunk)) or _text_refused(_credential_window(chunk))


def _flattened(text: str) -> str:
    return " ".join(text.split())


def _short_excerpt(text: str) -> str:
    flattened = _flattened(text)
    if len(flattened) <= SLEEP_EXCERPT_MAX:
        return flattened
    return flattened[:SLEEP_EXCERPT_MAX].rstrip()


def _credential_window(text: str) -> str:
    """Flattened text the commit door reads for one chunk.

    Cut at 160 characters. When that cut falls inside a whitespace-delimited
    token, keep the rest of the token. A later token is not in the excerpt
    a commit would store, so it does not withhold the source.
    """

    flattened = _flattened(text)
    if len(flattened) <= SLEEP_EXCERPT_MAX:
        return flattened
    cut = SLEEP_EXCERPT_MAX
    if flattened[cut].isspace() or flattened[cut - 1].isspace():
        return flattened[:cut].rstrip()
    end = flattened.find(" ", cut)
    if end == -1:
        return flattened
    return flattened[:end]


def _restrict_sidecar_mode(path: Path) -> None:
    """Tighten an existing sidecar that still has group or other bits."""

    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)


def count_sleep_proposals(path: Path, *, user_id: UUID | str) -> int:
    """How many sidecar rows belong to ``user_id``. Missing file is zero."""

    uid = str(user_id)
    return sum(1 for row in load_sleep_proposals(path) if str(row.get("user_id") or "") == uid)


def compile_sleep_proposal_listing(
    db_path: Path,
    *,
    user_id: UUID | str,
    effective_domains: tuple[str, ...],
    effective_sensitivity_allowed: tuple[str, ...],
    effective_project_scope: tuple[str, ...],
) -> str:
    """List the caller's proposals. Writes nothing.

    Listed oldest source first, by ``captured_at`` then id, the same order
    the sleep writer uses when it chooses sources. A source imported later
    with an earlier ``captured_at`` is listed before one imported earlier.
    The brief's domain, sensitivity, and project fences apply. The commit
    door runs again on the stored excerpt and on the cut window of the
    first chunk. A refusal is omitted, not deleted. A source that already
    has an active or accepted memory is omitted too. The commit arguments
    include that source's domain, sensitivity, and project scope. The
    commit line is ASCII-escaped. Rows left out are counted.
    """

    resolved = Path(db_path).expanduser().resolve()
    sidecar = sleep_proposals_path(resolved)
    rows = load_sleep_proposals(sidecar)
    ranked: list[tuple[tuple[str, str], str]] = []
    not_shown = 0
    with sqlite_user_connection(resolved, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        uid = store.user_id
        for row in rows:
            if str(row.get("user_id") or "") != uid:
                continue
            source_id = row.get("source_id")
            excerpt = row.get("excerpt")
            if not isinstance(source_id, str) or source_id == "" or not isinstance(excerpt, str):
                continue
            if _has_committed_fact(store, source_id):
                not_shown += 1
                continue
            source = store.get_source(source_id)
            if source is None or not _source_honours_fence(
                source,
                effective_domains=effective_domains,
                effective_sensitivity_allowed=effective_sensitivity_allowed,
                effective_project_scope=effective_project_scope,
            ):
                not_shown += 1
                continue
            window = _credential_window(_first_chunk_text(store, source_id))
            if _text_refused(excerpt) or _text_refused(window):
                not_shown += 1
                continue
            domain = source.get("domain")
            sensitivity = source.get("sensitivity")
            arguments = {
                "canonical_text": excerpt,
                "domain": domain if isinstance(domain, str) and domain != "" else "unknown",
                "project_scope": list(source_project_scope(source)),
                "sensitivity": sensitivity if isinstance(sensitivity, str) and sensitivity != "" else "unknown",
                "source_refs": [source_id],
                "title": excerpt[:120],
            }
            captured_at = source.get("captured_at")
            captured_key = captured_at if isinstance(captured_at, str) else ""
            ranked.append(
                (
                    (captured_key, source_id),
                    "\n".join(
                        (
                            f"source_id: {source_id}",
                            f"excerpt: {quote_session_brief_text(excerpt)}",
                            "alice_memory_commit: " + json.dumps(arguments, ensure_ascii=True, sort_keys=True),
                        )
                    ),
                )
            )
    ranked.sort(key=lambda item: item[0])
    blocks = [block for _order, block in ranked]
    if blocks:
        text = SESSION_BRIEF_FRAME + "\n\n" + "\n\n".join(blocks)
    else:
        text = NO_SLEEP_PROPOSALS
    if not_shown:
        text = f"{text}\nrows not shown: {not_shown}"
    return text


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        for row in rows
    )
    tmp_path = path.with_name(f"{path.name}.tmp")
    # exists() is false for a dangling symlink, and O_EXCL then fails every run.
    tmp_path.unlink(missing_ok=True)
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    except Exception:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    os.close(fd)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


__all__ = [
    "CAP_FULL_WAIT",
    "LIST_SOURCE_IDS_SQL",
    "NO_SLEEP_PROPOSALS",
    "PROPOSED_STATUS",
    "SLEEP_EXCERPT_MAX",
    "SLEEP_PROPOSAL_CAP",
    "SLEEP_PROPOSAL_FILENAME",
    "SleepError",
    "compile_sleep_proposal_listing",
    "count_sleep_proposals",
    "format_sleep_receipt",
    "load_sleep_proposals",
    "run_local_vault_sleep",
    "sleep_proposals_path",
]
