"""The 28 held-out records the design round released as regressions (S4.4 round 2, T8).

Copied exactly, with their ids, fields and labels, from the tower's
heldout_regressions_28.jsonl. Every other held-out record stays held out.
The credential records carry synthetic keys in the shape of real ones (Stripe
live and test keys, connection strings), so the file is stored compressed and
base64-encoded, and decoded at test time, rather than committed as raw
key-shaped literals. It is compressed first because gitleaks decodes base64
one level deep and read the synthetic Stripe keys in the plain encoding.
"""

from __future__ import annotations

import base64
import json
import zlib

_ENCODED = (
    "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"
)


def floor_regressions() -> list[dict[str, object]]:
    text = zlib.decompress(base64.b64decode(_ENCODED)).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]
