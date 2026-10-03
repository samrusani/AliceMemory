"""Shared builders for the per-project view and brief tests (spec S2).

``build_parity_vault`` writes the fixture vault whose v0.20.0 brief, hook output
and ``alice_resume`` result are pinned in ``fixtures_v0200_per_project_goldens``.
It uses only the store methods and the MCP capture tool that v0.20.0 already had,
so the goldens could be generated before any view code existed, and they were.
``make_repo`` (from ``project_identity_support``) writes a git layout by hand, so
no test starts ``git``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.project_identity_support import config_text, make_repo

USER_ID = "00000000-0000-0000-0000-000000000001"

#: Two ids that look like the ones the resolver derives. The tests read through a
#: ``ProjectView`` built from these, so no repository is needed for most of them.
PROJECT_A = "prj_" + "a1" * 8
PROJECT_B = "prj_" + "b2" * 8
SECONDARY_A = "prj_" + "c3" * 8

SENSITIVE_DOMAIN_LABELS = ("family", "health", "spiritual", "legal", "financial")


def context_for(data_dir: Path) -> MCPRuntimeContext:
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def db_path_for(data_dir: Path) -> Path:
    return resolve_db_path(data_dir=str(data_dir), db=None)


def add_memory(
    store: SQLiteVNextStore,
    *,
    key: str,
    text: str,
    domain: str = "project",
    sensitivity: str = "public",
    scope: tuple[str, ...] | None = None,
    memory_type: str = "semantic",
    status: str = "active",
    valid_to: str | None = None,
) -> dict[str, object]:
    """One committed memory. ``scope=None`` is a note with no project scope at all.

    ``valid_to`` closes the note's window (a past timestamp makes it expired); ``None`` leaves it open.
    """

    payload: dict[str, object] = {
        "memory_key": key,
        "memory_type": memory_type,
        "title": key,
        "canonical_text": text,
        "status": status,
        "confirmation_status": "confirmed",
        "domain": domain,
        "sensitivity": sensitivity,
        "value": {"text": text},
    }
    if valid_to is not None:
        payload["valid_to"] = valid_to
    if scope is not None:
        payload["project_scope"] = list(scope)
        payload["metadata_json"] = {"project_scope": list(scope)}
        if len(scope) == 1:
            payload["project_id"] = scope[0]
    return store.create_memory(payload)


def add_loop(
    store: SQLiteVNextStore,
    *,
    title: str,
    domain: str = "project",
    sensitivity: str = "public",
    scope: tuple[str, ...] | None = None,
) -> dict[str, object]:
    loop: dict[str, object] = {"title": title, "domain": domain, "sensitivity": sensitivity}
    if scope is not None:
        loop["metadata_json"] = {"project_scope": list(scope)}
        if len(scope) == 1:
            loop["project_id"] = scope[0]
    return store.create_open_loop(loop)


def capture(
    context: MCPRuntimeContext,
    raw_text: str,
    *,
    title: str,
    domain: str = "project",
    sensitivity: str = "public",
    project_scope: tuple[str, ...] | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "raw_text": raw_text,
        "title": title,
        "domain": domain,
        "sensitivity": sensitivity,
    }
    if project_scope is not None:
        arguments["project_scope"] = list(project_scope)
    payload = call_mcp_tool(context, name="alice_capture", arguments=arguments)
    assert payload["status"] == "imported", payload
    return payload


def build_parity_vault(data_dir: Path) -> MCPRuntimeContext:
    """The fixture vault of the byte-for-byte tests.

    No note carries an Alice project id except the three that say so. It holds
    unscoped notes, notes under the free-form name ``acme``, notes under two
    Alice project ids, global notes in each of the five sensitive domains, open
    loops of each kind and sources of each kind. Order of creation is fixed, and
    the brief lists newest first, so the output is the same on every run.
    """

    context = context_for(data_dir)
    database = db_path_for(data_dir)
    capture(
        context,
        "# Release runbook\n\nThe indigo runbook says to tag the release only after the gate is green.\n",
        title="Release runbook",
    )
    capture(
        context,
        "# Billing notes\n\nThe acme invoice batch runs at midnight and retries twice.\n",
        title="Acme billing notes",
        project_scope=("acme",),
    )
    capture(
        context,
        "# Payments design\n\nPayments retries use exponential backoff with jitter.\n",
        title="Payments design",
        project_scope=(PROJECT_A,),
    )
    capture(
        context,
        "# Clinic letter\n\nThe clinic letter says the follow up visit is in March.\n",
        title="Clinic letter",
        domain="health",
        sensitivity="private",
    )
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.style", text="We prefer small pull requests with one concern each.")
        add_memory(
            store,
            key="fact.acme.billing",
            text="Acme billing runs the invoice batch at midnight.",
            scope=("acme",),
        )
        add_memory(
            store,
            key="fact.health.bp",
            text="The owner takes a blood pressure reading every morning.",
            domain="health",
            sensitivity="private",
        )
        add_memory(
            store,
            key="fact.payments.retry",
            text="Payments retries use exponential backoff with jitter.",
            scope=(PROJECT_A,),
        )
        add_memory(
            store,
            key="fact.family.birthday",
            text="The owner's daughter has a birthday on the ninth of June.",
            domain="family",
            sensitivity="private",
        )
        add_memory(
            store,
            key="fact.search.reindex",
            text="Search service reindex runs nightly at two.",
            scope=(PROJECT_B,),
        )
        add_memory(
            store,
            key="fact.finance.budget",
            text="The household budget review happens on the first of each month.",
            domain="financial",
            sensitivity="private",
        )
        add_memory(
            store,
            key="fact.release.gate",
            text="The release gate must be green before any tag is pushed.",
            memory_type="decision",
        )
        add_memory(
            store,
            key="fact.legal.contract",
            text="The consulting contract renews each January.",
            domain="legal",
            sensitivity="private",
        )
        add_memory(
            store,
            key="fact.spiritual.retreat",
            text="The owner attends a quiet retreat every autumn.",
            domain="spiritual",
            sensitivity="private",
        )
        add_memory(
            store,
            key="fact.payments.decision",
            text="Payments decision: keep the ledger append only.",
            memory_type="decision",
            scope=(PROJECT_A,),
        )
        add_memory(
            store,
            key="fact.editor",
            text="The shared editor config lives in the dotfiles repository.",
            domain="personal",
        )
        add_memory(
            store,
            key="fact.todo.docs",
            text="Write the upgrade overview for the next release.",
            memory_type="commitment",
        )
        add_loop(store, title="Review the vendor security questionnaire")
        add_loop(store, title="Rotate the payments service signing key", scope=(PROJECT_A,))
        add_loop(store, title="Book the annual health check", domain="health", sensitivity="private")
        add_loop(store, title="Reply to the acme change request", scope=("acme",))
        add_loop(store, title="Plan the family trip", domain="family", sensitivity="private")
        add_loop(store, title="Draft the search reindex runbook", scope=(PROJECT_B,))
    return context


def repo_with_remote(root: Path, url: str = "https://example.com/acme/payments.git") -> Path:
    """A git layout written by hand, with an origin remote."""

    return make_repo(root, config=config_text(url))


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def normalize_result(value: object) -> object:
    """Replace ids and timestamps so two runs of one fixture compare equal."""

    if isinstance(value, dict):
        return {key: normalize_result(child) for key, child in sorted(value.items()) if key != "generated_at"}
    if isinstance(value, list):
        return [normalize_result(child) for child in value]
    if isinstance(value, str):
        return _STAMP.sub("<ts>", _UUID.sub("<id>", value))
    return value


def normalized_json(value: object) -> str:
    return json.dumps(normalize_result(value), sort_keys=True, indent=1, ensure_ascii=True)
