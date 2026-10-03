"""Product answer-grounding verification: an OPT-IN library seam, wired nowhere.

This module adapts the LongMemEval harness verification concept
(``eval/longmemeval/verification.py``) for product use: after a chat
layer has drafted an answer from an Alice context pack, an integrator
MAY ask a model to list the answer's concrete claims that the pack does
not support, and then gate or annotate the answer with the verdict.

NOTHING calls this module by default. No API route, MCP tool, or
retrieval path imports it; adding it to a deployment changes no
behavior until an integrator explicitly calls
:func:`verify_answer_grounding` in their own answering loop. The
harness copy stays byte-frozen and independent — this is a separate
product surface with its own template and its own tests.

Design constraints carried over from the harness (and asserted by
``tests/unit/test_vnext_answer_verification.py``):

- The verifier sees ONLY the pack-derived context block, the question,
  and the answer under test. There is no "expected answer" anywhere in
  this module's inputs, and no code path reads benchmark labels.
- Fail-open: a provider error or an unparseable reply yields a verdict
  with ``grounded=True`` and the failure recorded, so verification can
  only ever be a disclosed, inspectable filter — never a hidden crash
  or a silent rewrite. :func:`apply_answer_grounding_gate` returns the
  answer byte-identical unless a CLEAN verdict found a load-bearing
  ungrounded claim.
- Disclosure: every verdict's ``to_record()`` carries the template
  fingerprint and the verifier provider/model when known, so any run
  that used the gate can prove which prompt and model produced it.

Typical opt-in wiring (integrator code, not Alice code)::

    pack = VNextRetrievalService(store).compile_context_pack(request)
    answer = my_chat_layer.answer(question, pack)
    verdict = verify_answer_grounding(answer, pack, provider)
    final, gated = apply_answer_grounding_gate(answer, verdict)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import logging
import time

from alicebot_api.recall_framing import frame_rendered_block, writer_attribution
from alicebot_api.session_briefing import quote_session_brief_text
from alicebot_api.vnext_repositories import JsonObject


VERIFY_TEMPERATURE = 0.0
ANSWER_VERIFICATION_ERROR_CODE = "answer_verification_failed"

logger = logging.getLogger(__name__)

# What the gate substitutes when an integrator opts into gating and the
# verdict found a load-bearing fabrication. Deliberately plain: it
# claims nothing about WHY, only that stored memory does not back the
# drafted answer.
WITHHELD_ANSWER_TEXT = (
    "I don't have stored memories that support an answer to this; "
    "the drafted answer contained specifics my memory does not back."
)

# The product verifier prompt. Same claim taxonomy as the harness
# template (LOAD-BEARING vs INCIDENTAL, strict line format) but a
# separate constant: the harness template is byte-frozen for benchmark
# integrity and must never be coupled to product wording changes.
ANSWER_VERIFIER_PROMPT_TEMPLATE = (
    "You are verifying that an answer is grounded in its source context. "
    "Use only the context below; do not use outside knowledge.\n\n"
    "Context:\n{context}\n\n"
    "Question:\n{question}\n\n"
    "Answer to verify:\n{answer}\n\n"
    "List every specific factual claim in the answer (a name, number, date, "
    "place, or other concrete value) that the context does not state or "
    "directly support. Label a claim LOAD-BEARING if it is the direct answer "
    "to the question; label it INCIDENTAL otherwise. Use one line per claim, "
    "exactly:\n"
    "UNGROUNDED LOAD-BEARING: <claim>\n"
    "UNGROUNDED INCIDENTAL: <claim>\n"
    "If every claim is supported by the context, or the answer only says the "
    "information is unavailable, reply with exactly: GROUNDED"
)

ANSWER_VERIFIER_TEMPLATE_SHA256 = hashlib.sha256(
    ANSWER_VERIFIER_PROMPT_TEMPLATE.encode("utf-8")
).hexdigest()

GROUNDED_TOKEN = "GROUNDED"
_LOAD_BEARING_PREFIX = "ungrounded load-bearing:"
_INCIDENTAL_PREFIX = "ungrounded incidental:"

# Context-block rendering caps: the verifier needs the same evidence
# the answer was drafted from, not an unbounded dump.
_MAX_CONTEXT_ITEMS_PER_SECTION = 40
_MAX_ITEM_CHARS = 700


@dataclass(frozen=True, slots=True)
class UngroundedClaim:
    text: str
    load_bearing: bool

    def to_record(self) -> JsonObject:
        return {"text": self.text, "load_bearing": self.load_bearing}


@dataclass(frozen=True, slots=True)
class AnswerGroundingVerdict:
    """Outcome of one product verification call; always recorded in full."""

    grounded: bool
    ungrounded_claims: tuple[UngroundedClaim, ...]
    raw_response: str | None
    error: str | None
    parse_note: str | None
    latency_seconds: float | None
    provider: str | None
    model: str | None

    @property
    def gate_should_withhold(self) -> bool:
        """Withhold only on a CLEAN verdict with load-bearing failures (fail-open)."""
        return self.error is None and not self.grounded

    def to_record(self) -> JsonObject:
        return {
            "grounded": self.grounded,
            "ungrounded_claims": [claim.to_record() for claim in self.ungrounded_claims],
            "raw_response": self.raw_response,
            "error": self.error,
            "parse_note": self.parse_note,
            "latency_seconds": (
                round(self.latency_seconds, 3) if self.latency_seconds is not None else None
            ),
            "provider": self.provider,
            "model": self.model,
            "template_sha256": ANSWER_VERIFIER_TEMPLATE_SHA256,
        }


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > _MAX_ITEM_CHARS:
        return text[: _MAX_ITEM_CHARS - 1] + "…"
    return text


def _pack_rows(pack: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    rows = pack.get(key)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [row for row in rows if isinstance(row, Mapping)][:_MAX_CONTEXT_ITEMS_PER_SECTION]


def pack_question(pack: Mapping[str, object]) -> str:
    """The query the pack was compiled for (``query_interpretation.query``)."""
    interpretation = pack.get("query_interpretation")
    if isinstance(interpretation, Mapping):
        query = interpretation.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
    return ""


def render_pack_context_block(pack: Mapping[str, object]) -> str:
    """Deterministic text rendering of a context pack for the verifier.

    Mirrors what an answering layer can actually see: memory facts
    (title + canonical text), supporting-evidence excerpts, source
    titles, and the pack's own grounding notes. Stored notes are quoted
    under one framing line, and each stored item names its writer.
    Grounding notes are Alice's own sentences and stay outside the quotes.
    Read-only over the pack; no store access, no model calls.
    """
    lines: list[str] = []
    for memory in _pack_rows(pack, "relevant_memories"):
        title = str(memory.get("title") or "").strip()
        body = str(memory.get("canonical_text") or "").strip()
        writer = writer_attribution(memory)
        suffix = f" writer.id={writer['id']} writer.established={writer['established']}"
        if title and body:
            lines.append(
                f"- {quote_session_brief_text(_clip(title))}: {quote_session_brief_text(_clip(body))}{suffix}"
            )
        elif title or body:
            lines.append(f"- {quote_session_brief_text(_clip(title or body))}{suffix}")
    for evidence in _pack_rows(pack, "supporting_evidence"):
        excerpt = str(evidence.get("excerpt") or evidence.get("text") or evidence.get("quote") or "").strip()
        if excerpt:
            writer = writer_attribution(evidence)
            lines.append(
                "- Evidence: "
                f"{quote_session_brief_text(_clip(excerpt))} "
                f"writer.id={writer['id']} writer.established={writer['established']}"
            )
    for source in _pack_rows(pack, "sources"):
        title = str(source.get("title") or "").strip()
        if title:
            writer = writer_attribution(source)
            lines.append(
                f"- Source: {quote_session_brief_text(_clip(title))} "
                f"writer.id={writer['id']} writer.established={writer['established']}"
            )
    grounding_lines: list[str] = []
    grounding = pack.get("grounding")
    if isinstance(grounding, Mapping):
        unsupported = grounding.get("unsupported_entities")
        if isinstance(unsupported, Sequence) and not isinstance(unsupported, (str, bytes)):
            for name in unsupported:
                grounding_lines.append(f'- Note: no stored memories mention "{name}".')
    if not lines and not grounding_lines:
        return "(no stored memories were retrieved for this question)"
    if not lines:
        return "\n".join(grounding_lines)
    rendered = frame_rendered_block("\n".join(lines))
    if grounding_lines:
        return rendered + "\n" + "\n".join(grounding_lines)
    return rendered


def build_answer_verifier_prompt(
    *, question: str, answer_text: str, context_block: str
) -> str:
    """Fill the product verifier template. Context + question + answer only."""
    return ANSWER_VERIFIER_PROMPT_TEMPLATE.format(
        context=context_block,
        question=question,
        answer=answer_text,
    )


def parse_verifier_reply(text: str) -> tuple[bool, tuple[UngroundedClaim, ...], str | None]:
    """Deterministic parse of the strict reply format.

    Returns ``(grounded, claims, parse_note)``. ``grounded`` is False
    only when at least one LOAD-BEARING line is present. A reply with
    neither the GROUNDED token nor any UNGROUNDED line fails open as
    grounded, with a ``parse_note`` recorded for transparency.
    """
    claims: list[UngroundedClaim] = []
    grounded_marker = False
    for line in str(text).splitlines():
        stripped = line.strip().lstrip("-*").strip()
        lowered = stripped.casefold()
        if lowered == GROUNDED_TOKEN.casefold():
            grounded_marker = True
        elif lowered.startswith(_LOAD_BEARING_PREFIX):
            claim_text = stripped[len(_LOAD_BEARING_PREFIX) :].strip()
            if claim_text:
                claims.append(UngroundedClaim(text=claim_text, load_bearing=True))
        elif lowered.startswith(_INCIDENTAL_PREFIX):
            claim_text = stripped[len(_INCIDENTAL_PREFIX) :].strip()
            if claim_text:
                claims.append(UngroundedClaim(text=claim_text, load_bearing=False))
    parse_note = None
    if not claims and not grounded_marker:
        parse_note = "unrecognized verifier reply; failing open as grounded"
    grounded = not any(claim.load_bearing for claim in claims)
    return grounded, tuple(claims), parse_note


def _call_chat(chat_config: object, prompt: str) -> str:
    """Route one prompt through whatever chat seam the integrator handed us.

    Accepts a ``BrainModelProvider``-shaped object (``.chat(prompt=...,
    temperature=...)``, e.g. the providers in
    ``vnext_model_intelligence``) or a bare ``callable(prompt) -> str``.
    """
    chat = getattr(chat_config, "chat", None)
    if callable(chat):
        return str(chat(prompt=prompt, temperature=VERIFY_TEMPERATURE))
    if callable(chat_config):
        return str(chat_config(prompt))
    raise TypeError(
        "chat_config must expose .chat(prompt=..., temperature=...) or be callable(prompt)"
    )


def verify_answer_grounding(
    answer_text: str,
    pack: Mapping[str, object],
    chat_config: object,
) -> AnswerGroundingVerdict:
    """One product verification call. Never raises: failures fail open.

    The prompt sent through ``chat_config`` contains only the
    pack-derived context block, the pack's own question, and
    ``answer_text`` — there is no expected-answer input at all.
    """
    provider = getattr(chat_config, "provider", None)
    model = getattr(chat_config, "model", None)
    prompt = build_answer_verifier_prompt(
        question=pack_question(pack),
        answer_text=str(answer_text),
        context_block=render_pack_context_block(pack),
    )
    started = time.monotonic()
    try:
        reply = _call_chat(chat_config, prompt)
    except Exception as exc:  # noqa: BLE001 - fail-open: verification must never break answering
        logger.warning(
            "Answer-grounding verification failed open error_code=%s",
            ANSWER_VERIFICATION_ERROR_CODE,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return AnswerGroundingVerdict(
            grounded=True,
            ungrounded_claims=(),
            raw_response=None,
            error=ANSWER_VERIFICATION_ERROR_CODE,
            parse_note=None,
            latency_seconds=time.monotonic() - started,
            provider=str(provider) if provider is not None else None,
            model=str(model) if model is not None else None,
        )
    grounded, claims, parse_note = parse_verifier_reply(reply)
    return AnswerGroundingVerdict(
        grounded=grounded,
        ungrounded_claims=claims,
        raw_response=str(reply).strip(),
        error=None,
        parse_note=parse_note,
        latency_seconds=time.monotonic() - started,
        provider=str(provider) if provider is not None else None,
        model=str(model) if model is not None else None,
    )


def apply_answer_grounding_gate(
    answer_text: str,
    verdict: AnswerGroundingVerdict,
    *,
    withheld_text: str = WITHHELD_ANSWER_TEXT,
) -> tuple[str, bool]:
    """``(final_text, gate_applied)``: withhold only on clean load-bearing failures.

    A grounded verdict (or any error/fail-open verdict) returns
    ``answer_text`` byte-identical with ``gate_applied=False``.
    """
    if verdict.gate_should_withhold:
        return withheld_text, True
    return answer_text, False


__all__ = [
    "ANSWER_VERIFICATION_ERROR_CODE",
    "ANSWER_VERIFIER_PROMPT_TEMPLATE",
    "ANSWER_VERIFIER_TEMPLATE_SHA256",
    "AnswerGroundingVerdict",
    "GROUNDED_TOKEN",
    "UngroundedClaim",
    "VERIFY_TEMPERATURE",
    "WITHHELD_ANSWER_TEXT",
    "apply_answer_grounding_gate",
    "build_answer_verifier_prompt",
    "pack_question",
    "parse_verifier_reply",
    "render_pack_context_block",
    "verify_answer_grounding",
]
