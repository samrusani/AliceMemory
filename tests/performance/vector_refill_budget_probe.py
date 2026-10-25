"""Time one vector read of 10,000 hidden nearest rows, on the checkout named on the command line.

The checkout is put on the path before Alice is imported, so this file can time the merge base and the head with the
same rows. Each line on stdin is one call, and each call prints one JSON object: its wall and CPU seconds, the ids
returned, the limits the store was asked for in that call, and whether the checkout has the refill. ``stop`` or an
empty line ends it.

The rows carry plain labels and the store is a stub, so the numbers do not include the label walk of a row that has
inputs.
"""

import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HIDDEN = 10_000
WANT = 4
VISIBLE = 8


def main() -> None:
    repo = sys.argv[1]
    sys.path[:0] = [str(Path(repo) / "apps" / "api" / "src")]
    from alicebot_api.vnext_retrieval import VECTOR_STAGE_ENABLED, VNextRetrievalService

    rows = [
        {"id": f"hidden-{index}", "domain": "project", "sensitivity": "confidential", "status": "active", "canonical_text": "hidden"}
        for index in range(HIDDEN)
    ]
    rows.extend(
        {"id": f"visible-{index}", "domain": "project", "sensitivity": "public", "status": "active", "canonical_text": "visible", "vector_distance": 0.4}
        for index in range(VISIBLE)
    )

    class Store:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def search_memories_vector(self, *, limit, **_kwargs):
            self.limits.append(limit)
            return [dict(row) for row in rows[:limit]]

    store = Store()
    service = VNextRetrievalService(
        store,
        embedding_provider=SimpleNamespace(provider="synthetic", model="synthetic", base_url="http://127.0.0.1:9"),
    )
    refill = "label_refill" in inspect.signature(service._memory_vector_rows).parameters

    def once():
        store.limits.clear()
        started = time.perf_counter()
        cpu = time.process_time()
        found, _stage = service._memory_vector_rows(
            query="unrelated",
            domains=["project"],
            sensitivity_allowed=["public", "internal", "private", "unknown"],
            limit=WANT,
            query_vector=[1.0, 0.0],
            query_embedding_status=VECTOR_STAGE_ENABLED,
            **({"label_refill": True} if refill else {}),
        )
        wall, cpu = time.perf_counter() - started, time.process_time() - cpu
        return {"wall": wall, "cpu": cpu, "ids": [row["id"] for row in found], "limits": list(store.limits), "refill": refill}

    for line in sys.stdin:
        if line.strip() in ("stop", ""):
            break
        print(json.dumps(once()), flush=True)


if __name__ == "__main__":
    main()
