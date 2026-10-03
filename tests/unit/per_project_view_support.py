"""Builders for the per-project brief, exclusion and fill tests (spec S2, tests 18, 26, 36, 37, 75 to 78).

``per_project_s2_support`` holds the parity vault and the store helpers. This module
adds the pieces the view tests share: a project context without a repository, a
brief compiled through a view, and the exclusion vault whose every note carries a
canary word, so a test can say exactly which note reached the brief.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.project_identity import ProjectContext
from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import compile_session_brief, sensitive_global_exclusion
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import DEFAULT_AGENT_SENSITIVITY
from tests.unit.per_project_s2_support import (
    PROJECT_A,
    PROJECT_B,
    SECONDARY_A,
    SENSITIVE_DOMAIN_LABELS,
    USER_ID,
    add_loop,
    add_memory,
    capture,
    context_for,
    db_path_for,
)

__all__ = [
    "PROJECT_A",
    "PROJECT_B",
    "SECONDARY_A",
    "SENSITIVE_DOMAIN_LABELS",
    "USER_ID",
    "build_exclusion_vault",
    "compile_view_brief",
    "project_context",
    "project_view",
]


def project_context(
    ids: tuple[str, ...] = (PROJECT_A,),
    *,
    label: str = "payments",
    source: str = "remote",
) -> ProjectContext:
    return ProjectContext(ids=ids, label=label, source=source, start="argument")  # type: ignore[arg-type]


def project_view(
    ids: tuple[str, ...] = (PROJECT_A,),
    *,
    label: str = "payments",
    choice: str = "project",
) -> ProjectView:
    return ProjectView.for_project(project_context(ids, label=label), choice)  # type: ignore[arg-type]


def compile_view_brief(
    data_dir: Path,
    view: ProjectView,
    *,
    query: str | None = None,
    effective_domains: tuple[str, ...] = (),
    effective_sensitivity_allowed: tuple[str, ...] = DEFAULT_AGENT_SENSITIVITY,
    exclude_global_domains: frozenset[str] | None = None,
    reserve: int = 0,
) -> str:
    """The library brief for ``view``, the way the hook and ``alice-memory brief`` build it."""

    exclusion = sensitive_global_exclusion(view) if exclude_global_domains is None else exclude_global_domains
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return compile_session_brief(
            store,
            effective_domains=effective_domains,
            effective_sensitivity_allowed=effective_sensitivity_allowed,
            effective_project_scope=view.scope,
            project_view=view,
            exclude_global_domains=exclusion,
            query=query,
            reserve=reserve,
        )


def build_exclusion_vault(data_dir: Path) -> dict[str, object]:
    """A vault with global and project notes in each of the five sensitive domains.

    Every note holds one canary word, ``qz`` plus a kind letter, the domain and ``g`` or
    ``p``, so ``qzfhealthg`` is the global health fact and ``qzlfinancialp`` is the
    project's own financial open loop. Kinds: ``f`` fact, ``l`` open loop, ``s`` source.
    Plain notes use the domain ``project``. The project is ``PROJECT_A``, ``PROJECT_B`` is
    another project, and ``acme`` is a free-form name (a global note). The newest fact and
    the newest source are global notes in a held-back domain, so a brief that derived its
    excerpt query before the filter would be asked about them.
    """

    context = context_for(data_dir)
    database = db_path_for(data_dir)
    canaries: dict[str, str] = {}

    def canary(kind: str, domain: str, side: str) -> str:
        word = f"qz{kind}{domain}{side}"
        canaries[f"{kind}:{domain}:{side}"] = word
        return word

    # Sources first, so the facts and loops are newer than every source.
    for domain in SENSITIVE_DOMAIN_LABELS:
        word = canary("s", domain, "p")
        capture(
            context,
            f"# {domain} project source\n\nThe project {domain} runbook mentions {word} and the harbour ledger.\n",
            title=f"{domain} project source",
            domain=domain,
            sensitivity="private",
            project_scope=(PROJECT_A,),
        )
    word = canary("s", "project", "p")
    capture(
        context,
        f"# plain project source\n\nThe plain project runbook mentions {word} and the harbour ledger.\n",
        title="plain project source",
        project_scope=(PROJECT_A,),
    )
    word = canary("s", "project", "g")
    capture(
        context,
        f"# plain global source\n\nThe plain global runbook mentions {word} and the harbour ledger.\n",
        title="plain global source",
    )
    word = canary("s", "project", "b")
    capture(
        context,
        f"# other project source\n\nThe other project runbook mentions {word} and the harbour ledger.\n",
        title="other project source",
        project_scope=(PROJECT_B,),
    )
    for domain in SENSITIVE_DOMAIN_LABELS:
        word = canary("s", domain, "g")
        capture(
            context,
            f"# {domain} global source\n\nThe global {domain} runbook mentions {word} and the harbour ledger.\n",
            title=f"{domain} global source",
            domain=domain,
            sensitivity="private",
        )

    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for domain in SENSITIVE_DOMAIN_LABELS:
            add_loop(
                store,
                title=f"Open loop {canary('l', domain, 'p')} for the project",
                domain=domain,
                sensitivity="private",
                scope=(PROJECT_A,),
            )
        add_loop(store, title=f"Open loop {canary('l', 'project', 'p')} plain project", scope=(PROJECT_A,))
        add_loop(store, title=f"Open loop {canary('l', 'project', 'g')} plain global")
        add_loop(store, title=f"Open loop {canary('l', 'project', 'b')} other project", scope=(PROJECT_B,))
        for domain in SENSITIVE_DOMAIN_LABELS:
            add_loop(
                store,
                title=f"Open loop {canary('l', domain, 'g')} global",
                domain=domain,
                sensitivity="private",
            )
        for domain in SENSITIVE_DOMAIN_LABELS:
            add_memory(
                store,
                key=f"fact.{domain}.project",
                text=f"The project {domain} fact {canary('f', domain, 'p')} belongs to this project.",
                domain=domain,
                sensitivity="private",
                scope=(PROJECT_A,),
            )
        add_memory(
            store,
            key="fact.plain.project",
            text=f"The plain project fact {canary('f', 'project', 'p')} belongs to this project.",
            scope=(PROJECT_A,),
        )
        add_memory(
            store,
            key="fact.plain.other",
            text=f"The other project fact {canary('f', 'project', 'b')} belongs to another project.",
            scope=(PROJECT_B,),
        )
        add_memory(
            store,
            key="fact.plain.global",
            text=f"The plain global fact {canary('f', 'project', 'g')} belongs to no project.",
        )
        add_memory(
            store,
            key="fact.plain.acme",
            text=f"The free-form fact {canary('f', 'project', 'a')} is filed under the name acme.",
            scope=("acme",),
        )
        for domain in SENSITIVE_DOMAIN_LABELS:
            add_memory(
                store,
                key=f"fact.{domain}.global",
                text=f"The global {domain} fact {canary('f', domain, 'g')} follows the owner everywhere.",
                domain=domain,
                sensitivity="private",
            )
    return {"context": context, "canaries": canaries}
