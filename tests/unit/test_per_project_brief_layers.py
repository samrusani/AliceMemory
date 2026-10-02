"""Per-project memory S2: the project brief's fact read keeps expired notes out before the limit, and its held-back rule has two layers.

The project brief reads facts in one scan (``list_memories_view_partitions``) and then
filters the rows again in Python. Two rules are written twice on that path, so a test that
only looks at the finished brief cannot tell which copy held:

* Expiry. The SQL read takes ``include_expired=False`` so an expired note spends no place
  before the limit. A Python test (``_brief_omits_memory``) removes any expired row that
  still arrives, but only after the limit, so it cannot give the lost places back.
* The held-back rule for global notes in the sensitive domains. The SQL read leaves them
  out (``exclude_global_domains``) and ``_memory_honours_fence`` refuses them again.

Each layer alone is tested here, because dropping one of two layers leaves the finished
brief looking right and is the regression a plain end-to-end test misses. Each test names
the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api import session_briefing
from alicebot_api.session_briefing import (
    FACT_LIMIT,
    compile_session_brief,
    sensitive_global_exclusion,
)
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import DEFAULT_AGENT_SENSITIVITY
from tests.unit.per_project_s2_support import add_memory, context_for, db_path_for
from tests.unit.per_project_view_support import (
    PROJECT_A,
    PROJECT_B,
    SENSITIVE_DOMAIN_LABELS,
    USER_ID,
    project_view,
)

PAST = "2020-01-01T00:00:00Z"
HELD_BACK = frozenset(SENSITIVE_DOMAIN_LABELS)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_AGENT_API_KEY", "ALICE_MEMORY_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _vault(tmp_path: Path) -> Path:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    return data_dir


def _project_brief(store: object) -> str:
    """The library brief for the project view, built the way the hook and ``alice-memory brief`` build it."""

    view = project_view()
    return compile_session_brief(
        store,  # type: ignore[arg-type]
        effective_domains=(),
        effective_sensitivity_allowed=DEFAULT_AGENT_SENSITIVITY,
        effective_project_scope=view.scope,
        project_view=view,
        exclude_global_domains=sensitive_global_exclusion(view),
        query=None,
    )


class _SqlLayerGone:
    """A store whose single-scan fact read ignores the held-back rule, as if the SQL layer were deleted.

    Every other method is the real store's. The rows it returns can then hold a held-back global
    note, and only the Python recheck stands between those rows and the brief.
    """

    def __init__(self, store: SQLiteVNextStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> object:
        return getattr(self._store, name)

    def list_memories_view_partitions(self, **kwargs: object) -> object:
        return self._store.list_memories_view_partitions(**{**kwargs, "exclude_global_domains": ()})  # type: ignore[arg-type]


def test_expired_project_notes_do_not_use_up_the_places_of_the_project_brief(tmp_path: Path) -> None:
    """Mutation: pass ``include_expired=True`` in the ``list_memories_view_partitions`` call of
    ``compile_session_brief`` (the project read, not the unscoped one).

    The twelve newest project notes have a past ``valid_to``, under ten open project notes and three
    open global notes. The expiry test is part of the query, so the expired notes spend no place and the
    brief fills all ``FACT_LIMIT`` places: the newest open project notes, then the reserved global places.
    If the project read kept the expired rows, the limit would be spent on them and the Python filter
    would remove them afterwards, leaving a brief of three facts instead of eight. The unscoped brief has
    its own copy of this test in ``test_expired_memories_everywhere``; this is the project read's.
    """

    data_dir = _vault(tmp_path)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(3):
            add_memory(store, key=f"fact.global.{index}", text=f"Open global fact qzglobal{index}x")
        for index in range(10):
            add_memory(
                store, key=f"fact.open.{index}", text=f"Open project fact qzopen{index}x", scope=(PROJECT_A,)
            )
        for index in range(12):
            add_memory(
                store,
                key=f"fact.expired.{index}",
                text=f"Expired project fact qzexpired{index:02d}x",
                scope=(PROJECT_A,),
                valid_to=PAST,
            )
        brief = _project_brief(store)

    assert "qzexpired" not in brief
    assert brief.count("**fact**") == FACT_LIMIT, brief
    # floor(8 / 4) = 2 places are reserved for global notes; the other six are the newest open project notes.
    assert brief.count("**fact** (global)") == 2
    for index in range(4, 10):
        assert f"qzopen{index}x" in brief
    assert "qzopen3x" not in brief
    assert "qzglobal2x" in brief and "qzglobal1x" in brief


def test_held_back_global_facts_spend_no_place_of_the_project_brief(tmp_path: Path) -> None:
    """Mutation: pass ``exclude_global_domains=()`` in the fact read of ``compile_session_brief`` (the
    ``list_memories_view_partitions`` lambda), and leave the Python recheck alone. This is the SQL layer alone.

    ``FACT_LIMIT`` global facts in the sensitive domains are the newest notes of the vault, over ten plain
    global facts. The SQL layer leaves the held-back ones out before the limit, so all ``FACT_LIMIT`` places
    hold plain facts. Without it the eight newest global rows are all held back, the Python recheck removes
    every one, and what is left is whatever the recent-change merge can add (five at most): the brief loses
    three places and no held-back note shows, so only the count can tell. The recent-change merge reads
    its own events, which carry the exclusion too, so it cannot be the layer that is being measured.
    """

    data_dir = _vault(tmp_path)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for index in range(FACT_LIMIT + 2):
            add_memory(store, key=f"fact.plain.{index}", text=f"Plain global fact qzplain{index:02d}x")
        for index in range(FACT_LIMIT):
            add_memory(
                store,
                key=f"fact.held.{index}",
                text=f"Held back global fact qzheld{index}x in {SENSITIVE_DOMAIN_LABELS[index % 5]}",
                domain=SENSITIVE_DOMAIN_LABELS[index % 5],
                sensitivity="private",
            )
        brief = _project_brief(store)

    assert "qzheld" not in brief
    assert brief.count("**fact** (global)") == FACT_LIMIT, brief
    for index in range(2, FACT_LIMIT + 2):
        assert f"qzplain{index:02d}x" in brief


def test_the_python_recheck_removes_held_back_facts_when_the_sql_read_returns_them(tmp_path: Path) -> None:
    """Mutation: pass ``exclude_global_domains=frozenset()`` in the ``_memory_honours_fence`` call that filters
    ``facts`` in ``compile_session_brief``, or delete ``not _is_held_back(...)`` from ``_memory_honours_fence``.
    This is the Python layer alone.

    The store here ignores the exclusion in SQL (``_SqlLayerGone``), so the rows it hands back hold the two
    newest global notes, in health and legal. The recheck removes them. The rule is about material that follows a
    person into every project, so the project's own health note stays, and so does a plain global note. The control
    is the same vault through the real store, which gives the same brief: the recheck changes nothing there, which is
    why a test of the finished brief cannot see it.
    """

    data_dir = _vault(tmp_path)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.plain", text="Plain global fact qzplainx")
        add_memory(
            store,
            key="fact.project.health",
            text="The project's own health note qzprojecthealthx",
            domain="health",
            sensitivity="private",
            scope=(PROJECT_A,),
        )
        add_memory(store, key="fact.project.plain", text="Plain project fact qzprojectplainx", scope=(PROJECT_A,))
        add_memory(
            store, key="fact.global.health", text="Global health fact qzglobalhealthx", domain="health", sensitivity="private"
        )
        add_memory(
            store, key="fact.global.legal", text="Global legal fact qzgloballegalx", domain="legal", sensitivity="private"
        )
        control = _project_brief(store)
        blind = _project_brief(_SqlLayerGone(store))

    for brief in (control, blind):
        assert "qzglobalhealthx" not in brief and "qzgloballegalx" not in brief, brief
        assert "qzprojecthealthx" in brief and "qzprojectplainx" in brief and "qzplainx" in brief, brief


def test_the_fact_fence_holds_back_global_notes_in_the_sensitive_domains_and_only_those(tmp_path: Path) -> None:
    """Mutation: delete the ``not _is_held_back(...)`` clause of ``_memory_honours_fence``, or make ``_is_held_back``
    test the domain without testing that the note is global.

    The Python recheck read straight from its function, the way the resume by-id check is tested. A global note in a
    held-back domain is refused, and so is one under a free-form name (no Alice project id means global). A note of
    the project in the same domain stays, a global note in another domain stays, another project's note is refused by
    the project fence and not by this rule, and with nothing to hold back everything the other fences admit stays.
    """

    data_dir = _vault(tmp_path)
    view = project_view()
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        rows = {
            "global_health": add_memory(store, key="m.gh", text="gh", domain="health", sensitivity="private"),
            "free_form_health": add_memory(
                store, key="m.fh", text="fh", domain="health", sensitivity="private", scope=("acme",)
            ),
            "project_health": add_memory(
                store, key="m.ph", text="ph", domain="health", sensitivity="private", scope=(PROJECT_A,)
            ),
            "global_plain": add_memory(store, key="m.gp", text="gp"),
            "other_project_health": add_memory(
                store, key="m.oh", text="oh", domain="health", sensitivity="private", scope=(PROJECT_B,)
            ),
        }
        fetched = {name: store.get_memory(str(row["id"])) for name, row in rows.items()}

    def honoured(name: str, exclude: frozenset[str]) -> bool:
        row = fetched[name]
        assert row is not None
        return session_briefing._memory_honours_fence(  # type: ignore[attr-defined]
            row,
            effective_domains=(),
            effective_sensitivity_allowed=DEFAULT_AGENT_SENSITIVITY,
            effective_project_scope=view.scope,
            exclude_global_domains=exclude,
        )

    expected_with_rule = {
        "global_health": False,
        "free_form_health": False,
        "project_health": True,
        "global_plain": True,
        "other_project_health": False,
    }
    for name, expected in expected_with_rule.items():
        assert honoured(name, HELD_BACK) is expected, name
    # With nothing held back, only the project fence refuses: another project's note.
    for name in rows:
        assert honoured(name, frozenset()) is (name != "other_project_health"), name
