"""How stored notes are shown to a model.

MCP tool results quote each note and state the SessionStart framing
sentence once, as the first field of the tool text. Each item still
carries who wrote it. Quoting is ``quote_session_brief_text``: flatten
whitespace, then JSON-quote.

SessionStart, the HTTP context pack, CLI resume, and the answer-verifier
block already state that sentence once. This module does not make them
repeat it.

Attribution may read revisions and policy events that are already stored.
It does not write, and it does not rank.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from alicebot_api.session_briefing import SESSION_BRIEF_FRAME, quote_session_brief_text
from alicebot_api.store import JsonObject, JsonValue
from alicebot_api.vnext_agent_keys import AGENT_KEY_AUTH
from alicebot_api.vnext_json import json_safe

WRITER_OWNER = "owner"
# A keyless caller can declare agent_id "owner". That is not the owner.
# The owner label is only a call that recorded no agent id.
WRITER_DECLARED_OWNER = "declared-owner"
WRITER_ESTABLISHED_VERIFIED_BY_KEY = "verified_by_key"
WRITER_ESTABLISHED_DECLARED_ON_KEYLESS_INSTALL = "declared_on_keyless_install"

# In-place rewrites of the stored sentence. The writer is this revision,
# not the identity left on the original commit.
_TEXT_REWRITE_ACTIONS = frozenset(
    {
        "agentic_memory_correct",
        "agentic_memory_confirm_confirm",
        "agentic_memory_confirm_edit",
    }
)
_REWRITE_POLICY_ACTIONS = {
    "agentic_memory_correct": "memory.correct",
    "agentic_memory_confirm_confirm": "memory.confirm",
    "agentic_memory_confirm_edit": "memory.confirm",
}

# Model-facing prose. Identifiers, statuses, and scores are not in this set.
_MODEL_TEXT_KEYS = frozenset(
    {
        "title",
        "canonical_text",
        "summary",
        "description",
        "excerpt",
        "quote",
        "quote_new",
        "quote_belief",
        "text",
    }
)
# Supersession context nests memory titles in these lists. A memory row's
# supersedes value is a string id, which is left alone.
_NESTED_NOTE_LISTS = frozenset({"superseded_by", "supersedes"})


def _with_framing(body: str) -> str:
    if body == "":
        return ""
    return f"{SESSION_BRIEF_FRAME}\n{body}"


def frame_rendered_block(body: str) -> str:
    """Put the framing line above a block whose items are already quoted."""

    return _with_framing(body)


def frame_stored_notes(notes: Sequence[str]) -> str:
    """One framing line, then each note as quoted text."""

    body = "\n".join(quote_session_brief_text(note) for note in notes)
    return _with_framing(body)


def frame_stored_note(text: str) -> str:
    """A single text field a caller may paste into a prompt on its own.

    MCP tool results do not use this. They quote the item and state the
    sentence once on the result.
    """

    return frame_stored_notes((text,))


def with_result_framing(payload: Mapping[str, object]) -> dict[str, object]:
    """Put the framing sentence on an MCP tool result, once.

    Item text is already quoted. This does not copy the sentence into items.
    """

    rest = {key: value for key, value in payload.items() if key != "framing"}
    return {"framing": SESSION_BRIEF_FRAME, **rest}


def without_leading_framing_line(text: str) -> str:
    """Drop one leading framing line so a rendered string does not repeat it.

    ``_render_prefetch_context_text`` still frames a string that is pasted
    on its own. The MCP prefetch result states the sentence once, so the
    copy stored in that result does not keep a second copy.
    """

    prefix = f"{SESSION_BRIEF_FRAME}\n"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def serialize_mcp_tool_result(payload: Mapping[str, object]) -> str:
    """JSON text an MCP host hands to the model.

    When the result carries the framing sentence, that field is first.
    ``sort_keys`` would place ``count`` or ``brief`` ahead of it. Results
    that do not carry the sentence stay sorted, which is what the server
    already sent.
    """

    framing = payload.get("framing")
    if framing == SESSION_BRIEF_FRAME:
        rest = {key: value for key, value in payload.items() if key != "framing"}
        rest_text = json.dumps(rest, separators=(",", ":"), sort_keys=True)
        sentence = json.dumps(SESSION_BRIEF_FRAME)
        if rest_text == "{}":
            return '{"framing":' + sentence + "}"
        return '{"framing":' + sentence + "," + rest_text[1:]
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split()).strip()
    return stripped or None


def _declared_writer_id(agent_id: str) -> str:
    """An agent id a caller declared. ``owner`` is reserved for no identity."""

    if agent_id.casefold() == WRITER_OWNER:
        return WRITER_DECLARED_OWNER
    return agent_id


def _writer_label(*, writer_id: str, key_presented: bool) -> dict[str, str]:
    if writer_id == WRITER_OWNER or not key_presented:
        established = WRITER_ESTABLISHED_DECLARED_ON_KEYLESS_INSTALL
    else:
        established = WRITER_ESTABLISHED_VERIFIED_BY_KEY
    return {"id": writer_id, "established": established}


def writer_from_actor(
    *,
    actor_type: str | None,
    actor_id: str | None,
    key_presented: bool,
) -> dict[str, str]:
    """Label one call from its actor and whether that call presented a key."""

    if actor_type == "agent" and actor_id:
        return _writer_label(writer_id=_declared_writer_id(actor_id), key_presented=key_presented)
    return _writer_label(writer_id=WRITER_OWNER, key_presented=False)


def writer_attribution(row: Mapping[str, object] | None) -> dict[str, str]:
    """Who wrote this returned item, and how that identity was established.

    ``id`` is an agent id, or ``owner`` when the row has no agent id. ``established``
    is ``verified_by_key`` only when the stored identity record says the writer
    authenticated with an agent API key. Every other row, including an owner
    write and a keyless declared agent, is ``declared_on_keyless_install``.
    A row that never recorded an agent id is reported as the owner: that is
    how an owner write is stored. A declared agent id of ``owner`` is not that
    call. It is reported as ``declared-owner``.
    """

    source = row or {}
    metadata = _as_mapping(source.get("metadata_json")) or {}
    agentic = _as_mapping(metadata.get("agentic_memory")) or {}
    payload = _as_mapping(source.get("payload_json")) or _as_mapping(source.get("payload")) or {}
    identity = (
        _as_mapping(agentic.get("agent_identity"))
        or _as_mapping(metadata.get("agent_identity"))
        or _as_mapping(payload.get("agent_identity"))
        or {}
    )
    actor_type = _text(source.get("actor_type"))
    agent_id = (
        _text(source.get("created_by_agent_id"))
        or _text(identity.get("agent_id"))
        or _text(metadata.get("agent_id"))
    )
    if agent_id is None and actor_type == "agent":
        agent_id = _text(source.get("actor_id"))
    if actor_type in {"user", "system", "human"} and _text(source.get("created_by_agent_id")) is None:
        agent_id = None
    auth = _text(identity.get("auth"))
    if agent_id is None:
        return _writer_label(writer_id=WRITER_OWNER, key_presented=False)
    return _writer_label(
        writer_id=_declared_writer_id(agent_id),
        key_presented=auth == AGENT_KEY_AUTH,
    )


def _revision_text(revision: Mapping[str, object], key: str) -> str:
    value = revision.get(key)
    return value if isinstance(value, str) else ""


def _revision_changes_text(revision: Mapping[str, object]) -> bool:
    return _revision_text(revision, "text_before") != _revision_text(revision, "text_after")


def _parse_instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_payload(event: Mapping[str, object]) -> Mapping[str, object]:
    return _as_mapping(event.get("payload_json")) or _as_mapping(event.get("payload")) or {}


def _key_presented_for_revision(
    revision: Mapping[str, object],
    events: Sequence[object],
) -> bool:
    """True only when this rewrite's policy event says the call presented a key.

    A missing event is not a key. Owner calls write no policy event.
    """

    actor_type = _text(revision.get("actor_type"))
    actor_id = _text(revision.get("actor_id"))
    if actor_type != "agent" or actor_id is None:
        return False
    memory_id = str(revision.get("memory_id") or "")
    expected_action = _REWRITE_POLICY_ACTIONS.get(str(revision.get("action") or ""))
    revision_at = _parse_instant(revision.get("created_at"))
    chosen_auth: str | None = None
    chosen_at: datetime | None = None
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event_type") or "") != "policy.decision":
            continue
        if memory_id and str(event.get("target_id") or "") != memory_id:
            continue
        if _text(event.get("actor_id")) != actor_id:
            continue
        payload = _event_payload(event)
        decision = _as_mapping(payload.get("policy_decision")) or {}
        if expected_action is not None and _text(decision.get("action")) != expected_action:
            continue
        occurred = _parse_instant(event.get("occurred_at"))
        if revision_at is not None and occurred is not None and occurred > revision_at + timedelta(seconds=2):
            continue
        identity = _as_mapping(payload.get("agent_identity")) or {}
        auth = _text(identity.get("auth"))
        if chosen_at is None or (occurred is not None and occurred >= chosen_at):
            chosen_at = occurred or chosen_at
            chosen_auth = auth
    return chosen_auth == AGENT_KEY_AUTH


def _latest_text_rewrite(revisions: Sequence[object]) -> Mapping[str, object] | None:
    latest: Mapping[str, object] | None = None
    for revision in revisions:
        if not isinstance(revision, Mapping):
            continue
        if str(revision.get("action") or "") not in _TEXT_REWRITE_ACTIONS:
            continue
        if not _revision_changes_text(revision):
            continue
        latest = revision
    return latest


def memory_writer(store: object, memory: Mapping[str, object]) -> dict[str, str]:
    """Writer of the text a caller would read now.

    The original commit's identity stays only while no later call has
    replaced the sentence. After ``correct`` or ``confirm`` with new text,
    the writer is that revision's actor, and ``verified_by_key`` stays only
    when that call presented a key.
    """

    memory_id = memory.get("id")
    list_revisions = getattr(store, "list_revisions", None)
    if memory_id is None or not callable(list_revisions):
        return writer_attribution(memory)
    revisions = list_revisions(str(memory_id))
    if not isinstance(revisions, Sequence):
        return writer_attribution(memory)
    latest = _latest_text_rewrite(revisions)
    if latest is None:
        return writer_attribution(memory)
    events: Sequence[object] = ()
    list_events = getattr(store, "list_events", None)
    if callable(list_events):
        loaded = list_events(target_type="memory", target_id=str(memory_id))
        if isinstance(loaded, Sequence):
            events = loaded
    return writer_from_actor(
        actor_type=_text(latest.get("actor_type")),
        actor_id=_text(latest.get("actor_id")),
        key_presented=_key_presented_for_revision(latest, events),
    )


def _revision_nearest(
    store: object,
    memory_id: str,
    occurred_at: object,
) -> Mapping[str, object] | None:
    list_revisions = getattr(store, "list_revisions", None)
    if not callable(list_revisions):
        return None
    revisions = list_revisions(memory_id)
    if not isinstance(revisions, Sequence):
        return None
    target = _parse_instant(occurred_at)
    best: Mapping[str, object] | None = None
    best_gap: float | None = None
    for revision in revisions:
        if not isinstance(revision, Mapping):
            continue
        if target is None:
            best = revision
            continue
        instant = _parse_instant(revision.get("created_at"))
        if instant is None:
            continue
        gap = abs((instant - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best = revision
            best_gap = gap
    return best


def writer_for_recent_change(store: object, change: Mapping[str, object]) -> dict[str, str]:
    """Writer label for one recent-change event.

    The event's actor is the label. Mutation events often store
    ``actor_type`` and drop ``actor_id``; the revision written with that
    event still has the actor. A keyed agent is not reported as ``owner``.
    """

    actor_type = _text(change.get("actor_type"))
    actor_id = _text(change.get("actor_id"))
    target_id = _text(change.get("target_id"))
    payload_identity = _as_mapping(_event_payload(change).get("agent_identity")) or {}
    payload_auth = _text(payload_identity.get("auth"))
    if actor_id is None and actor_type == "agent" and target_id:
        revision = _revision_nearest(store, target_id, change.get("occurred_at"))
        if revision is not None:
            actor_type = _text(revision.get("actor_type")) or actor_type
            actor_id = _text(revision.get("actor_id"))
            if payload_auth is None and str(revision.get("action") or "") in _TEXT_REWRITE_ACTIONS:
                events: Sequence[object] = ()
                list_events = getattr(store, "list_events", None)
                if callable(list_events):
                    loaded = list_events(target_type="memory", target_id=target_id)
                    if isinstance(loaded, Sequence):
                        events = loaded
                return writer_from_actor(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    key_presented=_key_presented_for_revision(revision, events),
                )
    if actor_id is None and actor_type == "agent" and target_id:
        getter = getattr(store, "get_memory", None)
        memory = getter(target_id) if callable(getter) else None
        if isinstance(memory, Mapping):
            actor_id = _text(memory.get("created_by_agent_id"))
    key_presented = payload_auth == AGENT_KEY_AUTH
    if not key_presented and target_id and actor_id:
        getter = getattr(store, "get_memory", None)
        memory = getter(target_id) if callable(getter) else None
        if isinstance(memory, Mapping):
            current = memory_writer(store, memory)
            if current["id"] == _declared_writer_id(actor_id):
                return current
    return writer_from_actor(actor_type=actor_type, actor_id=actor_id, key_presented=key_presented)


def writer_for_returned_item(store: object, item: Mapping[str, object]) -> dict[str, str]:
    """Writer for one context-pack or review row. Text fields are not copied."""

    if item.get("event_type") is not None or item.get("event_id") is not None:
        return writer_for_recent_change(store, item)
    if (
        item.get("canonical_text") is not None
        or item.get("memory_type") is not None
        or item.get("memory_key") is not None
    ):
        return memory_writer(store, item)
    return writer_attribution(item)


_HTTP_WRITER_SECTIONS = (
    "relevant_memories",
    "relevant_beliefs",
    "decisions",
    "procedures",
    "current_known_state",
    "open_loops",
    "sources",
    "supporting_evidence",
    "contradicting_evidence",
    "recent_changes",
    "supersession_context",
)


def annotate_http_context_pack(store: object, pack: Mapping[str, object]) -> dict[str, object]:
    """Add framing and per-item writer. Leave every text field as stored."""

    annotated: dict[str, object] = dict(pack)
    annotated["framing"] = SESSION_BRIEF_FRAME
    for section in _HTTP_WRITER_SECTIONS:
        rows = pack.get(section)
        if not isinstance(rows, list):
            continue
        copied_rows: list[object] = []
        for item in rows:
            if not isinstance(item, Mapping):
                copied_rows.append(item)
                continue
            copied = dict(item)
            copied["writer"] = writer_for_returned_item(store, item)
            copied_rows.append(copied)
        annotated[section] = copied_rows
    return annotated


_DISCLOSED_TEXT_KEYS = _MODEL_TEXT_KEYS | frozenset({"text_before", "text_after"})


def frame_disclosed_tree(value: object) -> object:
    """Quote stored prose in a review or explain payload. Leave ids and status.

    The framing sentence is not copied into each field. The MCP result
    states it once.
    """

    if isinstance(value, list):
        return [frame_disclosed_tree(item) for item in value]
    if isinstance(value, Mapping):
        framed: dict[str, object] = {}
        for key, child in value.items():
            if key in _DISCLOSED_TEXT_KEYS and isinstance(child, str) and child.strip():
                framed[str(key)] = quote_session_brief_text(child)
            elif isinstance(child, Mapping | list):
                framed[str(key)] = frame_disclosed_tree(child)
            else:
                framed[str(key)] = child
        return framed
    return value


def frame_text_fields(item: Mapping[str, object]) -> dict[str, object]:
    """Copy ``item`` and quote its model-facing strings. Does not add writer.

    The framing sentence is not copied into the item. The MCP result states
    it once.
    """

    presented: dict[str, object] = {}
    for key, value in item.items():
        if key in _MODEL_TEXT_KEYS and isinstance(value, str) and value.strip():
            presented[key] = quote_session_brief_text(value)
        elif key in _NESTED_NOTE_LISTS and isinstance(value, list):
            presented[key] = [
                frame_text_fields(entry) if isinstance(entry, Mapping) else entry for entry in value
            ]
        else:
            presented[key] = value
    return presented


def _json_model_value(value: object) -> JsonValue:
    """Rebuild one value as JSON the same way the MCP boundary does.

    Compact rows are already JSON, so this is a copy for those. Datetimes and
    other row objects become the same strings ``json_safe`` would produce at
    the tool boundary, which keeps the returned object a ``JsonObject``.
    """

    normalized = json_safe(value)
    if normalized is None or isinstance(normalized, str | int | float | bool):
        return normalized
    if isinstance(normalized, list):
        return [_json_model_value(child) for child in normalized]
    if isinstance(normalized, dict):
        return {str(key): _json_model_value(child) for key, child in normalized.items()}
    raise TypeError(f"model item contains unsupported JSON value {type(normalized).__name__}")


def _json_model_object(value: Mapping[str, object]) -> JsonObject:
    normalized = _json_model_value(dict(value))
    if not isinstance(normalized, dict):
        raise TypeError("model item must be a JSON object")
    return normalized


def present_model_item(
    item: Mapping[str, object],
    *,
    source: Mapping[str, object] | None = None,
    writer: Mapping[str, str] | None = None,
) -> JsonObject:
    """Quote model-facing text and attach writer. ``source`` is the stored row."""

    presented = frame_text_fields(item)
    if writer is not None:
        presented["writer"] = {
            "id": str(writer.get("id") or WRITER_OWNER),
            "established": str(
                writer.get("established") or WRITER_ESTABLISHED_DECLARED_ON_KEYLESS_INSTALL
            ),
        }
    else:
        presented["writer"] = writer_attribution(source if source is not None else item)
    return _json_model_object(presented)


def present_model_items(items: object) -> list[object]:
    if not isinstance(items, list):
        return []
    presented: list[object] = []
    for item in items:
        if isinstance(item, Mapping):
            presented.append(present_model_item(item))
        else:
            presented.append(item)
    return presented


__all__ = [
    "WRITER_DECLARED_OWNER",
    "WRITER_ESTABLISHED_DECLARED_ON_KEYLESS_INSTALL",
    "WRITER_ESTABLISHED_VERIFIED_BY_KEY",
    "WRITER_OWNER",
    "annotate_http_context_pack",
    "frame_disclosed_tree",
    "frame_rendered_block",
    "frame_stored_note",
    "frame_stored_notes",
    "frame_text_fields",
    "memory_writer",
    "present_model_item",
    "present_model_items",
    "serialize_mcp_tool_result",
    "with_result_framing",
    "without_leading_framing_line",
    "writer_attribution",
    "writer_for_recent_change",
    "writer_for_returned_item",
    "writer_from_actor",
]
