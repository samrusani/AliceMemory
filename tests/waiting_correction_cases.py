"""Stored confirmation examples shared by the two store contracts."""

from copy import deepcopy

NON_PENDING_RECORDS = (
    "no-agentic-memory", "no-confirmation", "null-confirmation", "expired", "confirmed", "other-status",
)
DEADLINES = {
    "missing": None,
    "non-text": 123,
    "no-zone": "2000-01-01T00:00:00",
    "unreadable": "not a timestamp",
    "past": "2000-01-01T00:00:00+01:00",
}


def non_pending_metadata(metadata, shape):
    metadata = deepcopy(metadata)
    if shape == "no-agentic-memory":
        del metadata["agentic_memory"]
        return metadata

    agentic = metadata["agentic_memory"]
    if shape == "no-confirmation":
        del agentic["confirmation"]
    elif shape == "null-confirmation":
        agentic["confirmation"] = None
    else:
        agentic["confirmation"]["status"] = shape
    return metadata


def deadline_metadata(metadata, shape):
    metadata = deepcopy(metadata)
    record = metadata["agentic_memory"]["confirmation"]
    if shape == "missing":
        del record["expires_at"]
    else:
        record["expires_at"] = DEADLINES[shape]
    return metadata


def confirmation_record(row):
    return row["metadata_json"].get("agentic_memory", {}).get("confirmation")
