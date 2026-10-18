"""The command line withholds a hidden id on PostgreSQL, in every spelling of the id.

The dump used to run after the command's transaction ended, so the label query saw no rows and the id
came back. It now opens a fresh authenticated connection.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from uuid import uuid4

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


def _spellings(canonical: str) -> tuple[str, ...]:
    compact = canonical.replace("-", "")
    return (
        canonical,
        canonical.upper(),
        compact,
        compact.upper(),
        "{" + canonical + "}",
        "urn:uuid:" + canonical,
    )


def _run(argv: list[str]) -> tuple[int, str]:
    from alicebot_api.cli import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue() + err.getvalue()


def test_the_cli_withholds_every_spelling_of_a_hidden_id(label_harness, monkeypatch) -> None:
    harness = label_harness
    trusted_key = harness.key("trusted_local_agent")
    with harness.store() as store:
        hidden = store.create_memory(
            {
                "memory_key": f"cli-hidden-{uuid4().hex[:8]}",
                "canonical_text": "A confidential kiln note.",
                "title": "Hidden kiln",
                "status": "active",
                "domain": "project",
                "sensitivity": "confidential",
                "metadata_json": {},
            }
        )
        hidden_id = str(hidden["id"])
        shown = store.create_memory(
            {
                "memory_key": f"cli-shown-{uuid4().hex[:8]}",
                "canonical_text": "A public kiln note.",
                "title": "Shown kiln",
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"candidate_memory_ids": list(_spellings(hidden_id))},
            }
        )
        shown_id = str(shown["id"])
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id FROM agent_api_keys WHERE permission_profile = %s",
                ("trusted_local_agent",),
            )
            agent_id = cur.fetchone()["agent_id"]
    saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "DATABASE_ADMIN_URL", "ALICE_AGENT_API_KEY")}
    os.environ["DATABASE_URL"] = harness.urls["app"]
    os.environ["DATABASE_ADMIN_URL"] = harness.urls["admin"]
    try:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        code, owner = _run(["--user-id", str(harness.user_id), "vnext", "memories", "audit", shown_id])
        assert code == 0, owner
        assert hidden_id in owner
        monkeypatch.setenv("ALICE_AGENT_API_KEY", trusted_key)
        code, limited = _run(
            [
                "--user-id",
                str(harness.user_id),
                "vnext",
                "memories",
                "audit",
                shown_id,
                "--agent-id",
                agent_id,
                "--permission-profile",
                "trusted_local_agent",
            ]
        )
        assert code == 0, limited
        body = json.loads(limited)
        assert body["memory"]["id"] == shown_id
        for spelling in _spellings(hidden_id):
            assert spelling not in limited, spelling
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
