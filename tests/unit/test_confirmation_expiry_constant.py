"""The inline confirmation expiry is one named constant, and a confirmation is written with it.

The skill packs and the memory operations protocol tell agents that a pending confirmation lasts 24 hours. A later
test compares those copies with ``CONFIRMATION_EXPIRY_HOURS``; this one keeps the constant and the written
``expires_at`` in step.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from alicebot_api.vnext_memory_commit import CONFIRMATION_EXPIRY_HOURS
from tests.unit.test_default_surface_can_finish_confirmation_required import (  # noqa: F401  (fixture)
    _commit_pending,
    _context,
    _row,
    default_surface,
)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_the_documented_expiry_is_24_hours() -> None:
    """The number agents are told. Changing it changes a promise in the skill packs and the protocol page.

    Mutation: set ``CONFIRMATION_EXPIRY_HOURS`` to 12. This test fails.
    """

    assert CONFIRMATION_EXPIRY_HOURS == 24


def test_a_pending_confirmation_expires_after_the_named_number_of_hours(tmp_path: Path, default_surface) -> None:
    """A confirmation_required commit stores ``expires_at`` exactly ``CONFIRMATION_EXPIRY_HOURS`` after ``created_at``.

    Mutation: write ``timedelta(hours=23)`` in ``_create_confirmation`` instead of the constant. This test fails.
    """

    context = _context(tmp_path)
    pending = _commit_pending(context)
    confirmation = _row(context, str(pending["memory"]["id"]))["metadata_json"]["agentic_memory"]["confirmation"]
    assert _parse(confirmation["expires_at"]) - _parse(confirmation["created_at"]) == timedelta(
        hours=CONFIRMATION_EXPIRY_HOURS
    )
