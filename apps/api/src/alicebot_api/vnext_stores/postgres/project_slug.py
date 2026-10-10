"""The unique slug of a project, and the error a second project with the same slug raises."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from alicebot_api.store import ContinuityStoreInvariantError

# The unique constraint on (user_id, slug) of ``projects``, named by Postgres from the table and the columns.
PROJECT_SLUG_CONSTRAINT = "projects_user_id_slug_key"


class ProjectSlugConflictError(ContinuityStoreInvariantError):
    """A project with this slug already exists.

    The failed insert leaves the transaction aborted, so the caller rolls it back, as it does for any store error.
    The error holds no detail of the project that has the slug.
    """


@contextmanager
def project_slug_conflicts() -> Iterator[None]:
    """Read a unique violation on the slug as ``ProjectSlugConflictError``. Any other violation passes through."""

    try:
        yield
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != PROJECT_SLUG_CONSTRAINT:
            raise
        raise ProjectSlugConflictError("a project with this slug already exists") from exc
