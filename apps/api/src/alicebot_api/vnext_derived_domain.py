"""Keep derived rows behind the most restrictive input domain boundary."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS


def derived_domain(rows: Iterable[Mapping[str, object]], *, fallback: str) -> str:
    """Use the most frequent restricted input label; break ties by name.

    An explicit request domain cannot declassify an input. With no restricted
    inputs, each producer retains its existing domain selection.
    """
    counts = Counter(str(row.get('domain')) for row in rows if row.get('domain') in RESTRICTED_DOMAINS)
    return min(counts, key=lambda domain: (-counts[domain], domain)) if counts else fallback
