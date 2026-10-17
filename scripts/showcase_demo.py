"""Demo setup for a product agent that records a decision.

The search benchmark does not call this module. Its product key stays unbound,
which is a ``project_scoped_agent`` with no project. The demo that records
decisions calls ``prepare_demo_product``. That creates one product project and
binds the product key to it.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase

PRODUCT_PROJECT_NAME = "Quenby product"
PRODUCT_PROJECT_SLUG = "quenby-product"


def demo_product_binding(project_id: str) -> str | None:
    """The project scope stored on the product key."""

    return project_id


def prepare_demo_product(db_path: Path) -> str:
    """Create one product project and bind the product key to it.

    A second call keeps the same project. The search arms do not call this.
    """

    user_id = UUID(bench.DEFAULT_USER_ID)
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(db_path, user_id) as conn:
        project_id = _ensure_product_project(conn)
        scope = demo_product_binding(project_id)
        store = SQLiteVNextStore(conn, user_id)
        row = conn.execute(
            """
            SELECT id FROM agent_api_keys
            WHERE user_id = ? AND agent_id = ? AND revoked_at IS NULL
            """,
            (str(user_id), "showcase-product"),
        ).fetchone()
        if row is None:
            create_agent_key(
                store,
                user_id=user_id,
                agent_id="showcase-product",
                permission_profile=showcase.TEAM_PROFILES["product"],
                label="product",
                project_scope=scope,
            )
        else:
            conn.execute(
                "UPDATE agent_api_keys SET project_scope = ? WHERE id = ? AND user_id = ?",
                (scope, row["id"], str(user_id)),
            )
    return project_id


def _ensure_product_project(conn) -> str:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE
        )
        """
    )
    found = conn.execute(
        "SELECT id FROM projects WHERE slug = ?",
        (PRODUCT_PROJECT_SLUG,),
    ).fetchone()
    if found is not None:
        return str(found["id"])
    project_id = str(uuid4())
    conn.execute(
        "INSERT INTO projects (id, name, slug) VALUES (?, ?, ?)",
        (project_id, PRODUCT_PROJECT_NAME, PRODUCT_PROJECT_SLUG),
    )
    return project_id
