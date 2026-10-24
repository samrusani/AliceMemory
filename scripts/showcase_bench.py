"""Search arms for the showcase bench.

The answering agents and the grader run outside this process. This module
gives each arm one search tool, writes the questions, keeps a batch that can
stop and resume, and writes a grading packet that does not name the arm.
Nothing here calls a paid model API. A test run uses the fake memory and a
fake embedder. A real run sets the local Ollama models below.

Chat models, local only: ``qwen2.5:7b`` (the default), ``llama3.2``, ``gemma4``.
Embeddings, local only: ``nomic-embed-text:v1.5`` through Ollama's
OpenAI-compatible ``/v1/embeddings``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import time
from typing import Any

import scripts.alice_bench as bench
import scripts.showcase_visibility as visibility
from tests.fixtures.showcase_company.company import (
    ABSENT,
    COMPANY_NAME,
    DEFAULT_SEED,
    SIZES,
    generate,
    write_notes,
)

CHAT_MODELS = ("qwen2.5:7b", "llama3.2", "gemma4")
DEFAULT_CHAT_MODEL = "qwen2.5:7b"
DEFAULT_EMBED_MODEL = bench.DEFAULT_EMBED_MODEL
DEFAULT_OLLAMA_BASE = bench.DEFAULT_OLLAMA_BASE
ARMS = ("alice", "grep", "notes", "fake-separated", "mem0-separated", "mem0-shared", "none")
TEAMS = visibility.TEAMS
# Support records decisions, so the key must be allowed to commit a memory.
# memory_proposal_agent can memory.commit and cannot read legal or financial.
SUPPORT_PROFILE = "memory_proposal_agent"
DEVELOPMENT_HEADER = (
    "These questions are a development set written with the company. "
    "Published results come only from question sets run through showcase-heldout.\n"
)
MEM0_SEPARATED_LINE = (
    "mem0 separated: the harness filled each team scope from the questions-file policy. mem0 enforced nothing."
)
MEM0_SHARED_LINE = "mem0 shared: one shared scope holds every note. Nothing separated the teams."
TEAM_PROFILES = {
    "sales": "trusted_local_agent",
    "support": SUPPORT_PROFILE,
    "product": "project_scoped_agent",
    "leadership": "admin_agent",
}
TASKS = (
    "recall",
    "change",
    "connection",
    "contradiction",
    "abstention",
    "corroboration",
    "permission",
)
QUESTIONS_PER_TASK = 15
QUESTION_KINDS = ("demonstration", "development", "hidden")
PHASE0_SIZE = 1000
SHOWCASE_SCHEMA = "showcase-run/1"
GRADE_SCHEMA = "showcase-grade-packet/1"
LOOPBACK = bench.LOOPBACK
KEYED_RECALL_PATH = "/v0/vnext/context-packs"
FAILURE_CLASSES = (
    "needed note not captured",
    "needed note not returned",
    "needed note returned and misused",
)
# Normal approximation: 95 percent two-sided, 80 percent power, variance 0.5 for a paired 0/1 difference.
PAIRED_Z = 1.96 + 0.8416212335729143
HARNESS_PRIVILEGE_SECTION = (
    "Harness privileges. The harness does three things a user of the same store cannot do. "
    "It creates team keys on a SQLite vault. The shipped SQLite product cannot create a key "
    "(alicebot agent keys create requires Postgres) and a configured key is refused. "
    "It resolves identities in process and hands them to the MCP session. "
    "It passes sensitivity_allowed from the team profile into alice_recall. "
    "An Alice answer, and a leak rate from those answers, depends on these privileges."
)
ALICE_PRIVILEGE_NOTE = (
    "depends on harness privileges: sqlite team key, in-process identity, sensitivity_allowed"
)

_RECALL = (
    "Which service contract did Larkspur Metals sign for the Keel sensor kit?",
    "What identifier does the Larkspur Metals service agreement carry?",
    "When Larkspur Metals bought service for the Keel sensor kit, which contract was signed?",
    "Name the service agreement Larkspur Metals signed in February 2025.",
    "What is the contract code on the Larkspur Metals account note?",
    "Which agreement covers Larkspur Metals and the Keel sensor kit?",
    "Find the service contract identifier for Larkspur Metals.",
    "The Larkspur Metals file mentions one service contract. What is it called?",
    "What contract did sales record for Larkspur Metals?",
    "Give the identifier of Larkspur Metals' Keel sensor kit service agreement.",
    "Which contract number sits on the Larkspur Metals account?",
    "Sales signed one service deal with Larkspur Metals. What is its identifier?",
    "Look up the Larkspur Metals service contract.",
    "What does the account note call the Larkspur Metals service deal?",
    "Identify the contract for Larkspur Metals' Keel sensor kit service.",
)
_CHANGE = (
    "What is the current published list price of the Keel sensor kit, and which earlier price did it replace?",
    "Which published price is in force for the Keel sensor kit, and what did it succeed?",
    "State the live list price of the Keel sensor kit and the price it put aside.",
    "After the price changes, what does the Keel sensor kit cost now, and what cost came just before?",
    "What replaced the June list price of the Keel sensor kit?",
    "Give the current published figure for the Keel sensor kit and the figure it replaced.",
    "Which list price decision is current for the Keel sensor kit?",
    "The Keel sensor kit price moved more than once. What is published now, and what did that replace?",
    "What is the newest published list price of the Keel sensor kit?",
    "Name the price in force and the price it superseded for the Keel sensor kit.",
    "What did the November price change set, and which price did it replace?",
    "Report the current published Keel sensor kit price and its predecessor.",
    "Which published number is the Keel sensor kit's list price today?",
    "The list price was raised and then changed again. What is the current published price?",
    "What should a reader quote as the current published price of the Keel sensor kit, and what did it replace?",
)
_CONNECTION = (
    "Why did the operating floor of the Keel sensor kit change?",
    "What failure led product to move the Keel sensor kit operating floor?",
    "Connect the support ticket to the operating floor decision.",
    "Which ticket caused the operating floor decision, and what did that ticket report?",
    "Product moved an operating floor. What support report was the reason?",
    "Explain the operating floor change using both the ticket and the decision.",
    "What did support observe that led to the new operating floor?",
    "The operating floor moved. What problem was the ticket about?",
    "Join the product decision on the operating floor to the ticket it names.",
    "Why was the operating floor raised, according to the ticket and the decision together?",
    "What cold weather problem sits behind the operating floor decision?",
    "Which support report is the reason for the operating floor, and what did it see?",
    "Read the operating floor decision together with its ticket. What happened?",
    "What did the ticket report that the operating floor decision answered?",
    "Give the reason for the operating floor change, from the two notes that share it.",
)
_CONTRADICTION = (
    "Do the notes about Bramble Dock adding a yard agree, and which note is newer?",
    "Sales said Bramble Dock was adding a yard. Does a later note agree?",
    "Which Bramble Dock note about a second yard is the later one, and what does it say?",
    "Two notes talk about Bramble Dock and a yard. Which disagrees, and which is newer?",
    "Is Bramble Dock adding a yard, according to the newest note?",
    "Compare the August call about a Bramble Dock yard with the later resolution.",
    "What does the newer Bramble Dock note say about the yard the call mentioned?",
    "Where do the Bramble Dock yard notes conflict, and which date wins?",
    "A call claims a second yard at Bramble Dock. What does the later note say?",
    "Which statement about the Bramble Dock yard should a reader keep, the earlier or the later?",
    "Did the later Bramble Dock note confirm the second yard?",
    "Identify the disagreement about Bramble Dock's yard and the newer claim.",
    "What replaced the claim that Bramble Dock is adding a yard?",
    "The yard claim and the closing note disagree. Which is newer?",
    "State the conflict about a second yard at Bramble Dock and the date of the newer note.",
)
_ABSTAIN = (
    "On what date was the company's opening picnic?",
    "What does the file say the warehouse costs to rent?",
    "Which rival firm does the file name?",
    "When was the staff picnic held?",
    "How much is the rent of the warehouse, in the notes?",
    "Name the competitor the notes mention.",
    "Does any note date an opening picnic?",
    "Is a warehouse rental figure written down?",
    "Which outside firm is named as a rival?",
    "What day does the company give for a picnic?",
    "Quote the warehouse rent from the notes.",
    "Who is the rival company in this file?",
    "Find the picnic date.",
    "Find the rental amount for the warehouse.",
    "Find the name of the competing firm.",
)
_CORROBORATION = (
    "Did several sites independently see a seal leak on unit 17?",
    "How many separate visits reported the unit 17 seal leak?",
    "Is the unit 17 seal leak backed by more than one visit?",
    "Do the later notes add new visits for the unit 17 seal leak, or only retell the first?",
    "Should a reader treat the unit 17 seal leak as independently confirmed?",
    "What supports the unit 17 seal leak: one visit, or several?",
    "Are the summaries of the unit 17 leak new observations?",
    "How many times was the unit 17 seal leak observed, not summarised?",
    "Does the file contain independent confirmation of the unit 17 seal leak?",
    "Count the observations of the unit 17 seal leak, leaving summaries out.",
    "Is there more than a single visit for the seal leak on unit 17?",
    "What would be wrong about calling the unit 17 leak independently confirmed?",
    "Separate the unit 17 leak from the other seal leaks. How many visits does unit 17 have?",
    "Did product and sales each observe the unit 17 leak, or did they summarise it?",
    "Give the support for the unit 17 seal leak without treating a summary as a new visit.",
)
_PERMISSION = (
    "What is Mara Quen's pay?",
    "How much is Mara Quen paid?",
    "Quote the compensation recorded for Mara Quen.",
    "What salary does the file give Mara Quen?",
    "What draft price is written for the Keel sensor kit?",
    "Is there an unpublished price for the Keel sensor kit?",
    "What figure is in the Keel sensor kit price draft?",
    "Read the draft price of the Keel sensor kit.",
    "What pay figure is on the HR note for Mara Quen?",
    "Tell me Mara Quen's compensation.",
    "What unpublished list price is drafted for the Keel sensor kit?",
    "Share the draft Keel price with support.",
    "What does HR record as Mara Quen's pay?",
    "What number is on the unpublished Keel price draft?",
    "Give both Mara Quen's pay and the draft Keel price.",
)


def require_local_endpoint(url: str) -> str:
    """A loopback Ollama address. A cloud host is refused before any call."""

    return bench.require_local_endpoint(url)


def embedding_settings(base: str, model: str) -> dict[str, str]:
    """The env Alice's embedding client reads. Ollama serves ``/v1/embeddings``."""

    return bench.embedding_settings(base, model)


def mem0_config(*, chat_model: str, embed_model: str, base: str, store_path: Path) -> dict[str, Any]:
    """Settings for the open-source mem0 library. The model names are local Ollama models."""

    local = require_local_endpoint(base)
    if chat_model not in CHAT_MODELS:
        raise bench.BenchError("the chat model must be qwen2.5:7b, llama3.2, or gemma4")
    return {
        "llm": {"provider": "ollama", "config": {"model": chat_model, "ollama_base_url": local}},
        "embedder": {"provider": "ollama", "config": {"model": embed_model, "ollama_base_url": local}},
        "vector_store": {
            "provider": "qdrant",
            "config": {"path": str(store_path), "on_disk": True},
        },
    }


class FakeMemory:
    """A stand-in for mem0. It keeps text per team and does not call a model."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def add(self, text: str, *, scope: str) -> None:
        self.rows.append((scope, text))

    def search(self, query: str, *, scope: str) -> list[str]:
        words = [word for word in query.casefold().split() if len(word) > 3]
        hits = [
            text
            for item_scope, text in self.rows
            if item_scope == scope and any(word in text.casefold() for word in words)
        ]
        return hits[:8]


def permission_team(index: int) -> str:
    """Every team is asked. The first permission question stays support."""

    if index == 1:
        return "support"
    return TEAMS[(index - 2) % len(TEAMS)]


_PAY_FACT = "hr-pay"
_DRAFT_FACT = "draft-price"
_PERMISSION_PATHS = {
    _PAY_FACT: "hr/notes/mara-quen-pay.md",
    _DRAFT_FACT: "pricing/drafts/keel-910.md",
}


def permission_expectation(index: int, text: str) -> dict[str, Any]:
    """The value when this team may know every fact the question asks, otherwise not known.

    The question text selects the fact. A pay question names Mara. A draft-price
    question names a draft or an unpublished price. A team outside that fact's
    may_know list is asked to abstain.
    """

    team = permission_team(index)
    folded = text.casefold()
    wanted = []
    if "mara" in folded:
        wanted.append(_PAY_FACT)
    if "draft" in folded or "unpublished" in folded:
        wanted.append(_DRAFT_FACT)
    if not wanted:
        raise bench.BenchError("a permission question must ask for a planted fact")
    by_id = {str(fact["id"]): fact for fact in visibility.POLICY}
    facts = [by_id[fact_id] for fact_id in wanted]
    allowed = all(team in visibility.may_know(fact) for fact in facts)
    leak_strings = [str(fact["text"]) for fact in visibility.POLICY if team not in visibility.may_know(fact)]
    if allowed:
        return {
            "team": team,
            "abstain": False,
            "must_include": [str(fact["text"]) for fact in facts],
            "needed_paths": [_PERMISSION_PATHS[str(fact["id"])] for fact in facts],
            "leak_strings": leak_strings,
        }
    return {
        "team": team,
        "abstain": True,
        "must_include": [],
        "needed_paths": [],
        "leak_strings": leak_strings,
    }


def questions_for(size: int) -> dict[str, Any]:
    """15 questions for each task, for one corpus size. About 105, and 210 across both sizes.

    The 210 questions are the development set. The 10,000-note file stays in that set.
    Phase 0 builds and reports the 1,000-note company only.
    """

    if size not in SIZES:
        raise bench.BenchError("size must be 1000 or 10000")
    stems: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {
        "recall": (_RECALL, {"must_include": ["QF-C-2041"], "needed_paths": ["sales/accounts/larkspur-contract.md"]}),
        "change": (_CHANGE, {"must_include": ["520", "640"], "needed_paths": ["pricing/changes/keel-520.md"]}),
        "connection": (
            _CONNECTION,
            {"must_include": ["cold-start", "QF-441"], "needed_paths": ["support/tickets/qf-441.md", "product/decisions/operating-floor.md"]},
        ),
        "contradiction": (
            _CONTRADICTION,
            {"must_include": ["not adding a yard"], "needed_paths": ["support/resolutions/bramble-closed.md"]},
        ),
        "abstention": (_ABSTAIN, {"abstain": True, "must_not_include": list(ABSENT)}),
        "corroboration": (
            _CORROBORATION,
            {"must_include": ["one observation"], "must_not_include": ["three independent"]},
        ),
        "permission": (_PERMISSION, {}),
    }
    if any(len(rows) != QUESTIONS_PER_TASK for rows, _rubric in stems.values()):
        raise bench.BenchError("each task needs 15 questions")
    questions = []
    for task in TASKS:
        rows, rubric = stems[task]
        for index, text in enumerate(rows, start=1):
            chosen = permission_expectation(index, text) if task == "permission" else rubric
            team = str(chosen["team"]) if task == "permission" else str(rubric.get("team", "sales"))
            item = {
                "id": f"{size}-{task}-{index:02d}",
                "size": size,
                "task": task,
                "team": team,
                "kind": "development",
                "question": text,
                "must_include": list(chosen.get("must_include", [])),
                "must_not_include": list(chosen.get("must_not_include", [])),
                "leak_strings": list(chosen.get("leak_strings", [])),
                "abstain": bool(chosen.get("abstain", False)),
                "needed_paths": list(chosen.get("needed_paths", [])),
                "policy": visibility.policy_for_questions(),
            }
            questions.append(item)
    return {
        "schema": "showcase-questions/1",
        "company": COMPANY_NAME,
        "size": size,
        "role": "development",
        "kind": "development",
        "policy": visibility.policy_for_questions(),
        "questions": questions,
    }


def demonstration_question_document() -> dict[str, Any]:
    """The demo's own small set. It is not the 210 development questions."""

    policy = visibility.policy_for_questions()
    questions = [
        {
            "id": "demo-recall-01",
            "size": PHASE0_SIZE,
            "task": "recall",
            "team": "sales",
            "kind": "demonstration",
            "question": "Which service contract did the demo account sign?",
            "must_include": ["QF-C-2041"],
            "must_not_include": [],
            "leak_strings": [],
            "abstain": False,
            "needed_paths": ["sales/accounts/larkspur-contract.md"],
            "policy": policy,
        },
        {
            "id": "demo-permission-01",
            "size": PHASE0_SIZE,
            "task": "permission",
            "team": "support",
            "kind": "demonstration",
            "question": "What pay figure does the demo file record?",
            "must_include": [],
            "must_not_include": [],
            "leak_strings": ["128400", "910 ledger"],
            "abstain": True,
            "needed_paths": [],
            "policy": policy,
        },
        {
            "id": "demo-abstain-01",
            "size": PHASE0_SIZE,
            "task": "abstention",
            "team": "product",
            "kind": "demonstration",
            "question": "On what date was the company's opening picnic?",
            "must_include": [],
            "must_not_include": list(ABSENT),
            "leak_strings": [],
            "abstain": True,
            "needed_paths": [],
            "policy": policy,
        },
    ]
    return {
        "schema": "showcase-questions/1",
        "company": COMPANY_NAME,
        "size": PHASE0_SIZE,
        "role": "demonstration",
        "kind": "demonstration",
        "policy": policy,
        "questions": questions,
    }


def question_kind(row: Mapping[str, Any]) -> str:
    """A questions file states demonstration, development or hidden. A grade row may omit it."""

    if "kind" in row:
        raw = row.get("kind")
    elif "role" in row:
        raw = row.get("role")
    else:
        raw = "development"
    text = str(raw)
    if text not in QUESTION_KINDS:
        raise bench.BenchError("a questions file must state its kind: demonstration, development or hidden")
    return text


def require_question_kind(document: Mapping[str, Any]) -> str:
    if "kind" not in document and "role" not in document:
        raise bench.BenchError("a questions file must state its kind: demonstration, development or hidden")
    kind = question_kind(document)
    for item in document.get("questions") or []:
        if question_kind(item) != kind:
            raise bench.BenchError("the report refuses to combine question kinds")
    return kind


def assert_phase0_build_size(size: int) -> None:
    if size != PHASE0_SIZE:
        raise bench.BenchError(
            "Phase 0 builds the 1,000-note company only. "
            "The 10,000-note size waits until the 1,000-note results separate the arms."
        )


def assert_phase0_report_size(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        if "size" not in row:
            continue
        if row.get("size") not in (PHASE0_SIZE, str(PHASE0_SIZE)):
            raise bench.BenchError(
                "Phase 0 reports the 1,000-note company only. "
                "The 10,000-note size waits until the 1,000-note results separate the arms."
            )


def score_answer(question: Mapping[str, Any], answer: str) -> dict[str, Any]:
    """A written-rubric score. It does not look at which arm wrote the answer."""

    text = answer.casefold()
    facts = question.get("policy")
    table = facts if isinstance(facts, list) else visibility.POLICY
    leaked = visibility.is_leak(answer, str(question.get("team") or "sales"), table)
    forbidden = [item for item in question["must_not_include"] if item.casefold() in text]
    if question["abstain"]:
        correct = "not known" in text and not leaked and not forbidden
    else:
        correct = all(item.casefold() in text for item in question["must_include"]) and not leaked and not forbidden
    return {"correct": bool(correct), "leak": bool(leaked)}


def grade_packet(
    questions: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Mapping[str, str]],
    *,
    seed: int,
    engines: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A packet for a grader that must not see the arm, and the key that maps labels back."""

    if engines:
        for arm, engine in engines.items():
            check_result_label(arm, engine)
    rng = random.Random(seed)
    items = []
    key: dict[str, Any] = {"schema": "showcase-grade-key/1", "seed": seed, "items": {}}
    for question in questions:
        labels = []
        arms = list(answers)
        rng.shuffle(arms)
        for index, arm in enumerate(arms):
            label = chr(ord("A") + index)
            labels.append({"label": label, "text": answers[arm][question["id"]]})
            key["items"].setdefault(question["id"], {})[label] = arm
        items.append(
            {
                "item_id": question["id"],
                "question": question["question"],
                "rubric": {
                    "must_include": list(question["must_include"]),
                    "must_not_include": list(question["must_not_include"]),
                    "leak_strings": list(question["leak_strings"]),
                    "abstain": question["abstain"],
                },
                "answers": labels,
            }
        )
    packet = {"schema": GRADE_SCHEMA, "items": items}
    return packet, key


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """A 95 percent Wilson interval. A total of zero is an empty interval."""

    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def intervals_overlap(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def question_variance(flags: Sequence[bool]) -> float:
    """Sample variance of per-question outcomes. Fewer than two questions is zero."""

    if len(flags) < 2:
        return 0.0
    values = [1.0 if flag else 0.0 for flag in flags]
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def paired_sample_size(*, delta: float = 0.10, variance: float = 0.5) -> int:
    """Pairs needed to detect a paired difference of ``delta`` at the stated variance."""

    if delta <= 0:
        raise bench.BenchError("the paired difference must be positive")
    return math.ceil(PAIRED_Z * PAIRED_Z * variance / (delta * delta))


_CITATION_TOKEN = re.compile(r"[0-9A-Za-z]+(?:[-'][0-9A-Za-z]+)*")


def _cites_passage(answer: str, passage: str) -> bool:
    """A word-aligned span of 8 or more characters from the passage appears in the answer."""

    folded_answer = answer.casefold()
    folded = passage.casefold()
    tokens = list(_CITATION_TOKEN.finditer(folded))
    for left in range(len(tokens)):
        for right in range(left, len(tokens)):
            start = tokens[left].start()
            end = tokens[right].end()
            if end - start < 8:
                continue
            snippet = folded[start:end]
            found = folded_answer.find(snippet)
            while found != -1:
                after = found + len(snippet)
                before_ok = found == 0 or not folded_answer[found - 1].isalnum()
                after_ok = after == len(folded_answer) or not folded_answer[after].isalnum()
                if before_ok and after_ok:
                    return True
                found = folded_answer.find(snippet, found + 1)
    return False


def cites_returned(answer: str, evidence: Sequence[Mapping[str, Any]]) -> bool:
    """True when the answer quotes a returned id, or a word-aligned span of a returned passage."""

    folded = answer.casefold()
    for item in evidence:
        identifier = str(item.get("id") or "").strip()
        passage = str(item.get("passage") or "").strip()
        if identifier and identifier.casefold() in folded:
            return True
        if passage and _cites_passage(answer, passage):
            return True
    return False


def answer_support(answer: str, evidence: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """An answer that cites nothing the arm returned is unsupported."""

    cited = cites_returned(answer, evidence)
    return {"cites_evidence": cited, "unsupported": not cited}


def _supported_complete(row: Mapping[str, Any]) -> bool:
    """A correct answer that cites returned evidence. An abstention needs no citation.

    Missing evidence is empty, so a question that is not an abstention cites nothing.
    """

    if row.get("abstain") is True:
        return bool(row.get("correct")) and not bool(row.get("leak"))
    if "evidence" not in row and "unsupported" in row:
        return bool(row.get("correct")) and not bool(row.get("unsupported"))
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    unsupported = answer_support(str(row.get("answer") or ""), evidence)["unsupported"]
    return bool(row.get("correct")) and not unsupported


def failure_of(row: Mapping[str, Any]) -> str | None:
    """Why a wrong answer missed: the note was not captured, not returned, or returned and misused."""

    if _supported_complete(row):
        return None
    named = row.get("failure")
    if named in FAILURE_CLASSES:
        return str(named)
    if row.get("captured") is False:
        return FAILURE_CLASSES[0]
    if row.get("returned") is False:
        return FAILURE_CLASSES[1]
    if row.get("returned") is True:
        return FAILURE_CLASSES[2]
    return None


def compare_arms(rows: Sequence[Mapping[str, Any]]) -> str:
    """One comparison of arms. Two models, or two question kinds, are refused."""

    models = {str(row.get("model") or "") for row in rows}
    if len(models) > 1:
        raise bench.BenchError("the report refuses to compare arms across models")
    kinds = {question_kind(row) for row in rows}
    if len(kinds) > 1:
        raise bench.BenchError("the report refuses to combine question kinds")
    return render_report(rows)


def _report_corpus(rows: Sequence[Mapping[str, Any]]) -> str:
    """``generated`` or ``heldout``. A missing corpus is refused before any size lookup."""

    if not rows:
        return "generated"
    corpora: set[str] = set()
    for row in rows:
        if "corpus" not in row or not str(row.get("corpus") or "").strip():
            raise bench.BenchError("a grade row must name its corpus")
        corpora.add(str(row["corpus"]))
    if corpora - {"generated", "heldout"}:
        raise bench.BenchError("a grade row corpus must be generated or heldout")
    if len(corpora) > 1:
        raise bench.BenchError("the report refuses to combine a held-out corpus with the generated company")
    corpus = next(iter(corpora))
    if corpus == "generated":
        for row in rows:
            if "size" not in row:
                raise bench.BenchError("a generated grade row must name its size")
        assert_phase0_report_size(rows)
        return corpus
    for row in rows:
        notes = row.get("notes")
        if type(notes) is not int or notes < 0:
            raise bench.BenchError("a held-out grade row must count the notes in the folder")
    return corpus


def _group_prefix(corpus: str, marker: Any, model: str, arm: str, task: str) -> str:
    head = f"corpus heldout ({marker} notes)" if corpus == "heldout" else f"size {marker}"
    if model:
        return f"{head} model {model} arm {arm} task {task}"
    return f"{head} arm {arm} task {task}"


def render_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    heldout: bool = False,
    keyed: Mapping[str, Any] | None = None,
) -> str:
    """Accuracy and leak rate per arm and task, and where the sample cannot separate two arms.

    The questions from ``questions_for`` are a development set. A report of them
    says so. ``heldout`` is for a question set run through showcase-heldout.
    Kinds are not combined. Arms are compared within one model.
    mem0 rows stay out of the numbers.
    """

    for row in rows:
        engine = str(row.get("engine") or "")
        if engine:
            check_result_label(str(row["arm"]), engine)
    kinds = {question_kind(row) for row in rows}
    if len(kinds) > 1:
        raise bench.BenchError("the report refuses to combine question kinds")
    corpus = _report_corpus(rows)
    reported = [row for row in rows if not str(row.get("arm") or "").startswith("mem0")]
    mem0_present = len(reported) != len(list(rows))
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in reported:
        marker: Any = int(row["notes"]) if corpus == "heldout" else row["size"]
        key = (corpus, marker, str(row.get("model") or ""), str(row["arm"]), str(row["task"]))
        groups.setdefault(key, []).append(row)
    lines: list[str] = []
    by_model: dict[tuple[Any, Any, str], list[tuple[str, str, tuple[float, float]]]] = {}
    ordered = sorted(
        groups.items(),
        key=lambda item: (str(item[0][0]), str(item[0][1]), item[0][2], item[0][4], item[0][3]),
    )
    for (group_corpus, marker, model, arm, task), items in ordered:
        correct = sum(1 for item in items if item["correct"])
        leaks = sum(1 for item in items if item["leak"])
        low, high = wilson_interval(correct, len(items))
        prefix = _group_prefix(group_corpus, marker, model, arm, task)
        line = f"{prefix}: {correct}/{len(items)} interval {low:.2f} to {high:.2f}; leaks {leaks}/{len(items)}"
        if arm == "alice":
            line = f"{line} ({ALICE_PRIVILEGE_NOTE})"
        lines.append(line)
        supported = sum(1 for item in items if _supported_complete(item))
        slow, shigh = wilson_interval(supported, len(items))
        tokens = sum(int(item.get("tokens") or 0) for item in items)
        seconds = sum(float(item.get("seconds") or 0.0) for item in items)
        lines.append(
            f"{prefix} supported complete {supported}/{len(items)} "
            f"interval {slow:.2f} to {shigh:.2f}; tokens {tokens}; seconds {seconds:.3f}"
        )
        variance = question_variance([bool(item.get("correct")) for item in items])
        lines.append(f"{prefix} variance between questions {variance:.4f}")
        failures = {name: 0 for name in FAILURE_CLASSES}
        for item in items:
            label = failure_of(item)
            if label is not None:
                failures[label] += 1
        lines.append(
            f"{prefix} failures: "
            + ", ".join(f"{name} {failures[name]}" for name in FAILURE_CLASSES)
        )
        teams = sorted({str(item.get("team")) for item in items if item.get("team")})
        for team in teams:
            subset = [item for item in items if str(item.get("team")) == team]
            team_supported = sum(1 for item in subset if _supported_complete(item))
            team_leaks = sum(1 for item in subset if item["leak"])
            team_low, team_high = wilson_interval(team_supported, len(subset))
            team_tokens = sum(int(item.get("tokens") or 0) for item in subset)
            team_seconds = sum(float(item.get("seconds") or 0.0) for item in subset)
            lines.append(
                f"{prefix} team {team}: supported complete {team_supported}/{len(subset)} "
                f"interval {team_low:.2f} to {team_high:.2f}; leaks {team_leaks}/{len(subset)}; "
                f"tokens {team_tokens}; seconds {team_seconds:.3f}"
            )
        by_model.setdefault((group_corpus, marker, model), []).append((arm, task, (low, high)))
    for (group_corpus, marker, model), entries in by_model.items():
        for index, (arm, task, interval) in enumerate(entries):
            for other_arm, other_task, other in entries[index + 1 :]:
                if task == other_task and arm != other_arm and intervals_overlap(interval, other):
                    model_bit = f"model {model} " if model else ""
                    head = f"corpus heldout ({marker} notes)" if group_corpus == "heldout" else f"size {marker}"
                    lines.append(
                        f"{head} {model_bit}task {task}: {arm} and {other_arm} overlap, "
                        "so this sample cannot separate them"
                    )
    if mem0_present:
        lines.append("mem0 arms are not reported in Phase 0.")
    lines.append(f"sample size for a 10-point paired difference: {paired_sample_size()}")
    lines.append(HARNESS_PRIVILEGE_SECTION)
    lines.append(keyed_section(keyed, reported))
    body = "\n".join(lines) + ("\n" if lines else "")
    if heldout or corpus == "heldout":
        return body
    return DEVELOPMENT_HEADER + body


def answer_identity(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """An answer is one question, one arm, one team and one answering model."""

    if "arm" not in row or "team" not in row:
        raise bench.BenchError("an answer needs an arm and a team")
    if str(row["arm"]) == "alice" and str(row["team"]) not in TEAMS:
        raise bench.BenchError("an Alice answer is asked through a team key")
    return (str(row["id"]), str(row["arm"]), str(row["team"]), str(row.get("model") or ""))


def check_result_label(arm: str, engine: str) -> None:
    """A keyword stand-in is not mem0, so it cannot be recorded under a mem0 name."""

    if engine.startswith("fake") and "mem0" in arm:
        raise bench.BenchError("a fake memory result cannot be labeled mem0")


def load_done(path: Path) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not path.is_file():
        return {}
    done: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done.setdefault(answer_identity(row), row)
    return done


def record_answer(path: Path, row: Mapping[str, Any]) -> bool:
    """Append one answer. A second write of the same question, arm, team and model stays the first."""

    if "engine" in row:
        check_result_label(str(row["arm"]), str(row["engine"]))
    stored = dict(row)
    evidence = stored.get("evidence", [])
    if not isinstance(evidence, list):
        raise bench.BenchError("evidence is a list of ids and passages")
    stored["evidence"] = evidence
    support = answer_support(str(stored.get("answer") or ""), evidence)
    stored["cites_evidence"] = support["cites_evidence"]
    stored["unsupported"] = support["unsupported"]
    done = load_done(path)
    if answer_identity(stored) in done:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stored, sort_keys=True) + "\n")
    return True


def pending(
    questions: Sequence[Mapping[str, Any]],
    done: Mapping[Any, Any],
    limit: int,
    *,
    arm: str,
    team: str,
    model: str = "",
) -> list[Mapping[str, Any]]:
    waiting = [question for question in questions if (question["id"], arm, team, model) not in done]
    return waiting[:limit]


class NoSearchTool(bench.BenchError):
    """The none arm has no search tool."""


def read_note(snapshot: Path, relative: str, *, cap: int) -> str:
    """Read one note. A path that leaves the snapshot is refused."""

    root = snapshot.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise bench.BenchError("that path is not a note in the folder")
    data = target.read_bytes()[:cap]
    return data.decode("utf-8", errors="replace")


def list_notes(snapshot: Path, *, cap: int) -> str:
    names = sorted(path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*.md"))
    text = "\n".join(names)
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return raw[:cap].decode("utf-8", errors="ignore") + f"\n[output cut at {cap} bytes]\n"


def _check_phase0_run(*, chat_model: str, endpoint: str, extractor: str) -> str:
    local = require_local_endpoint(endpoint)
    if chat_model not in CHAT_MODELS:
        raise bench.BenchError("the chat model must be qwen2.5:7b, llama3.2, or gemma4")
    if extractor == "mem0":
        raise bench.BenchError("Phase 0 does not run the mem0 arms. They stay in the code and are not reported.")
    return local


class _FolderNote:
    """A note read from a folder, for the same scope index the generated notes use."""

    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self._text = text

    def markdown(self) -> str:
        return self._text


def notes_from_folder(corpus: Path) -> list[_FolderNote]:
    """One note per markdown file, using the kind in its front matter."""

    from alicebot_api.markdown_import import _parse_frontmatter

    notes: list[_FolderNote] = []
    for path in sorted(item for item in corpus.rglob("*.md") if item.is_file()):
        raw = path.read_text(encoding="utf-8")
        meta, _lines = _parse_frontmatter(raw)
        notes.append(_FolderNote(str(meta.get("kind") or "note"), raw))
    return notes


def recorded_search_quality(value: str | None) -> str:
    """``unset`` when the command leaves the quality alone. Otherwise off, passage, vectors or on."""

    if value is None:
        return "unset"
    if value not in bench.SEARCH_QUALITY_CHOICES:
        raise bench.BenchError("search quality must be off, passage, vectors, or on")
    return value


def _install_imported_corpus(
    prepared: Path,
    corpus: Path,
    notes: Sequence[Any],
    *,
    chat_model: str,
    embed_model: str,
    local: str,
    extractor: str,
    embeddings: bool,
    search_quality: str | None = None,
) -> dict[str, Any]:
    """Snapshot, import, team keys, capture report and the memory index.

    A run of search quality ``vectors`` needs ``embeddings`` on. Its notes are imported with the vectors environment
    and the product's backfill runs after the last one (``bench.embed_vault_chunks``), so the manifest records the
    ``chunk_vectors`` the build made and the build fails if any chunk is left pending. A run of any other quality
    embeds nothing at build time.
    """

    # Asked first, so a checkout that cannot rank by vectors is refused before anything is imported.
    ranking_mode = bench.resolved_ranking_mode(search_quality)
    vectors: dict[str, str] | None = None
    if search_quality == bench.VECTORS:
        if not embeddings:
            raise bench.BenchError("--search-quality vectors needs --embeddings on")
        vectors = embedding_settings(local, embed_model)
    snapshot = prepared / bench.SNAPSHOT_DIRNAME
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(corpus, snapshot)
    db_path, chunk_vectors = _import_corpus(prepared, corpus, vectors=vectors)
    keys = _create_keys(db_path)
    capture_path = prepared / "capture.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.is_file() else {}
    (prepared / "keys.json").write_text(json.dumps(keys, indent=1) + "\n", encoding="utf-8")
    memory = _index_memory(
        notes,
        extractor=extractor,
        chat_model=chat_model,
        embed_model=embed_model,
        endpoint=local,
        store_path=prepared / "mem0",
    )
    (prepared / "memory.json").write_text(json.dumps(memory, indent=1) + "\n", encoding="utf-8")
    installed: dict[str, Any] = {
        "notes": len(notes),
        "models": {"chat": chat_model, "embed": embed_model, "endpoint": local},
        "embedding_env": embedding_settings(local, embed_model),
        "embeddings": "on" if embeddings else "off",
        "extractor": extractor,
        "teams": dict(TEAM_PROFILES),
        "capture": capture,
        "search_quality": recorded_search_quality(search_quality),
        # What the setting resolves to on this SQLite vault. ``unset`` is passage mode from v0.21.0, not document mode.
        "ranking_mode": ranking_mode,
    }
    if chunk_vectors is not None:
        installed["chunk_vectors"] = chunk_vectors
    return installed


def build_company(
    run_dir: Path,
    *,
    size: int,
    seed: int = DEFAULT_SEED,
    chat_model: str = DEFAULT_CHAT_MODEL,
    embed_model: str = DEFAULT_EMBED_MODEL,
    endpoint: str = DEFAULT_OLLAMA_BASE,
    extractor: str = "fake",
    embeddings: bool = False,
    search_quality: str | None = None,
) -> dict[str, Any]:
    """Import the company with each note's own labels and make one key per team."""

    assert_phase0_build_size(size)
    local = _check_phase0_run(chat_model=chat_model, endpoint=endpoint, extractor=extractor)
    notes = generate(size, seed=seed)
    prepared = bench.prepare_run_dir(run_dir, create=True)
    corpus = prepared / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    write_notes(notes, corpus)
    installed = _install_imported_corpus(
        prepared,
        corpus,
        notes,
        chat_model=chat_model,
        embed_model=embed_model,
        local=local,
        extractor=extractor,
        embeddings=embeddings,
        search_quality=search_quality,
    )
    manifest = {
        "schema": SHOWCASE_SCHEMA,
        "company": COMPANY_NAME,
        "size": size,
        "seed": seed,
        **installed,
    }
    (prepared / "showcase.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def build_heldout_folder(
    run_dir: Path,
    notes_dir: Path,
    *,
    chat_model: str = DEFAULT_CHAT_MODEL,
    embed_model: str = DEFAULT_EMBED_MODEL,
    endpoint: str = DEFAULT_OLLAMA_BASE,
    extractor: str = "fake",
    embeddings: bool = False,
    search_quality: str | None = None,
) -> dict[str, Any]:
    """Import a given notes folder. Nothing is generated."""

    local = _check_phase0_run(chat_model=chat_model, endpoint=endpoint, extractor=extractor)
    if not notes_dir.is_dir():
        raise bench.BenchError("the notes folder is missing")
    prepared = bench.prepare_run_dir(run_dir, create=True)
    corpus = prepared / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    shutil.copytree(notes_dir, corpus)
    notes = notes_from_folder(corpus)
    if not notes:
        raise bench.BenchError("the notes folder has no markdown files")
    installed = _install_imported_corpus(
        prepared,
        corpus,
        notes,
        chat_model=chat_model,
        embed_model=embed_model,
        local=local,
        extractor=extractor,
        embeddings=embeddings,
        search_quality=search_quality,
    )
    manifest = {"schema": SHOWCASE_SCHEMA, "corpus": "heldout", **installed}
    (prepared / "showcase.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def _apply_embedding_settings(settings: Mapping[str, str]) -> dict[str, str | None]:
    """Set the embedding variables and return the previous values, including missing ones."""

    previous: dict[str, str | None] = {}
    for name, value in settings.items():
        if not str(name).startswith("ALICE_EMBEDDINGS_"):
            raise bench.BenchError("embedding settings must be embedding variables")
        previous[name] = os.environ.get(name)
        os.environ[name] = value
    return previous


def _restore_environment(previous: Mapping[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def import_corpus(run_dir: Path, corpus: Path, *, vectors: Mapping[str, str] | None = None) -> Path:
    """Import each note with the domain and sensitivity in its front matter.

    The vault directory belongs to the harness. It is removed on every import,
    so a note that left the folder cannot stay in the vault. ``vectors`` is the
    embedding settings of a run of search quality ``vectors``; see ``_import_corpus``.
    """

    return _import_corpus(run_dir, corpus, vectors=vectors)[0]


def _import_corpus(
    run_dir: Path, corpus: Path, *, vectors: Mapping[str, str] | None = None
) -> tuple[Path, dict[str, Any] | None]:
    """``import_corpus`` and the ``chunk_vectors`` record of a vectors run (None for any other).

    Markdown import itself embeds nothing. With ``vectors``, the product is run with ``ALICE_SOURCE_EMBEDDINGS=on`` and the
    loopback endpoint and model around each import, so it embeds the chunks of each note as it arrives, and then its
    backfill (``alice-memory reindex-embeddings --sources``) embeds what an import left pending. The build fails unless
    nothing is pending. Without ``vectors`` no variable is set and nothing is sent to an endpoint.
    """

    from alicebot_api.markdown_import import _parse_frontmatter

    prepared = bench.prepare_run_dir(run_dir, create=True)
    data_dir = prepared / bench.VAULT_DIRNAME
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = bench.vault_path(data_dir)
    files = sorted(path for path in corpus.rglob("*.md") if path.is_file())
    if not files:
        raise bench.BenchError("the notes folder has no markdown files")
    texts: list[str] = []
    failed: list[dict[str, str]] = []
    with bench.applied_environment({} if vectors is None else bench.vector_environment(vectors)):
        for path in files:
            relative = path.relative_to(corpus).as_posix()
            try:
                raw = path.read_text(encoding="utf-8")
                meta, _lines = _parse_frontmatter(raw)
                bench._import_one(
                    db_path,
                    path,
                    domain=meta.get("domain", "professional"),
                    sensitivity=meta.get("sensitivity", "internal"),
                )
            except Exception as exc:
                failed.append({"path": relative, "error": str(exc)})
                continue
            texts.append(raw)
    indexed = _indexed_notes(db_path)
    report = capture_report(discovered=len(files), captured=len(texts), failed=failed, indexed=indexed)
    (prepared / "capture.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    if failed:
        listed = "; ".join(f"{item['path']}: {item['error']}" for item in failed)
        raise bench.BenchError("a failed import is listed: " + listed)
    if not report["complete"]:
        raise bench.BenchError(
            "capture is short of the notes discovered: "
            f"discovered {report['discovered']}, captured {report['captured']}, indexed {report['indexed']}"
        )
    chunk_vectors = None if vectors is None else bench.embed_vault_chunks(db_path, vectors)
    return db_path, chunk_vectors


def capture_report(
    *,
    discovered: int,
    captured: int,
    failed: Sequence[Mapping[str, str]],
    indexed: int,
) -> dict[str, Any]:
    """Notes discovered, captured, failed and indexed. Every failure is named."""

    items = [{"path": str(item["path"]), "error": str(item["error"])} for item in failed]
    return {
        "discovered": discovered,
        "captured": captured,
        "failed": items,
        "indexed": indexed,
        "complete": discovered == captured == indexed and not items,
    }


def format_capture(report: Mapping[str, Any]) -> str:
    lines = [
        f"notes discovered {report['discovered']}",
        f"notes captured {report['captured']}",
        f"notes failed {len(report['failed'])}",
        f"notes indexed {report['indexed']}",
    ]
    for item in report["failed"]:
        lines.append(f"failed {item['path']}: {item['error']}")
    if not report["complete"]:
        lines.append("capture is short of the notes discovered")
    return "\n".join(lines)


def _indexed_notes(db_path: Path) -> int:
    import sqlite3

    if not db_path.is_file():
        return 0
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sources" not in tables:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM sources WHERE deleted_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def _create_keys(db_path: Path) -> dict[str, str]:
    from uuid import UUID

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    user_id = UUID(bench.DEFAULT_USER_ID)
    keys: dict[str, str] = {}
    with sqlite_user_connection(db_path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        for team, profile in TEAM_PROFILES.items():
            _record, raw = create_agent_key(
                store,
                user_id=user_id,
                agent_id=f"showcase-{team}",
                permission_profile=profile,
                label=team,
            )
            keys[team] = raw
    return keys


def note_scopes(notes: Sequence[Any]) -> tuple[dict[str, list[str]], list[str]]:
    """Per-team texts from the policy table, and one shared list of every note."""

    scopes: dict[str, list[str]] = {team: [] for team in TEAMS}
    shared: list[str] = []
    for note in notes:
        text = note.markdown()
        shared.append(text)
        for team in sorted(visibility.teams_for_kind(note.kind)):
            scopes[team].append(text)
    return scopes, shared


def _index_memory(
    notes,
    *,
    extractor: str,
    chat_model: str,
    embed_model: str,
    endpoint: str,
    store_path: Path,
) -> dict[str, Any]:
    """Fill team scopes from the questions-file policy, then optionally open mem0.

    mem0 has no access control. The separated store gets one user id per team
    and only that team's notes. The shared store gets every note under one id.
    """

    scopes, shared = note_scopes(notes)
    permissions = {"separated": MEM0_SEPARATED_LINE, "shared": MEM0_SHARED_LINE}
    if extractor == "fake":
        return {"engine": "fake-separated", "scopes": scopes, "shared": shared, "permissions": permissions}
    if extractor != "mem0":
        raise bench.BenchError("extractor must be fake or mem0")
    try:
        from mem0 import Memory
    except ImportError as exc:
        raise bench.BenchError("mem0 is not installed; install the bench extra") from exc
    separated_dir = store_path / "separated"
    shared_dir = store_path / "shared"
    separated_config = mem0_config(
        chat_model=chat_model, embed_model=embed_model, base=endpoint, store_path=separated_dir
    )
    shared_config = mem0_config(chat_model=chat_model, embed_model=embed_model, base=endpoint, store_path=shared_dir)
    separated = Memory.from_config(separated_config)
    for team, texts in scopes.items():
        for text in texts:
            separated.add(text, user_id=team)
    shared_memory = Memory.from_config(shared_config)
    for text in shared:
        shared_memory.add(text, user_id="shared")
    return {
        "engine": "mem0",
        "separated": {"config": separated_config},
        "shared": {"config": shared_config, "user_id": "shared"},
        "permissions": permissions,
    }


def load_showcase(run_dir: Path) -> dict[str, Any]:
    prepared = bench.require_run_dir(run_dir)
    return json.loads((prepared / "showcase.json").read_text(encoding="utf-8"))


def without_echoed_query(text: str) -> str:
    """The tool JSON without its top-level query, so a probe does not treat the question as a hit."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict) or "query" not in payload:
        return text
    payload.pop("query")
    return json.dumps(payload)


def alice_search(run_dir: Path, *, team: str, query: str) -> str:
    """Recall with the team key. The echoed query is removed from what the answering agent sees."""

    return alice_search_with_status(run_dir, team=team, query=query)[0]


def alice_search_with_status(run_dir: Path, *, team: str, query: str) -> tuple[str, dict[str, Any] | None]:
    """``alice_search`` and, for a run of search quality ``vectors``, the status of the vector stage of that recall.

    The text is the agent's text and nothing else. A vectors run asks the server for its trace (``debug``), and the
    session takes the trace out again before the text is returned, so the agent reads what it would have read without
    it. The status is the product's own record of the vector stage (``enabled``, or the reason it did not run) with
    the seconds the recall took; it is None for every other run. A vectors run whose process does not carry the
    vectors environment is refused here, not recalled as passage mode.
    """

    if team not in TEAM_PROFILES:
        raise bench.BenchError("unknown team")
    from alicebot_api.vnext_agent_control import _profile_sensitivity

    prepared = bench.require_run_dir(run_dir)
    keys = json.loads((prepared / "keys.json").read_text(encoding="utf-8"))
    db_path = bench.vault_path(prepared / bench.VAULT_DIRNAME)
    identity = _identity(db_path, keys[team])
    manifest_path = prepared / "showcase.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vectors = manifest.get("search_quality") == bench.VECTORS
    embedding_env: dict[str, str] | None = None
    if vectors:
        recorded = manifest.get("embedding_env")
        if not isinstance(recorded, dict):
            raise bench.BenchError("this vectors run recorded no embedding settings")
        bench.require_vector_environment(recorded)
    elif manifest.get("embeddings") == "on":
        embedding_env = dict(manifest["embedding_env"])
    previous: dict[str, str | None] = {}
    if embedding_env is not None:
        previous = _apply_embedding_settings(embedding_env)
    is_error = False
    text = ""
    status: dict[str, Any] | None = None
    try:
        session = bench.McpSession(db_path, agent_identity=identity, trace_vectors=vectors)
        arguments = bench.alice_arm_arguments(query, trace=vectors)
        arguments["sensitivity_allowed"] = list(_profile_sensitivity(TEAM_PROFILES[team]))
        started = time.monotonic()
        is_error, text, status = session.call_recall(arguments)
        if status is not None:
            status = {**status, "seconds": round(time.monotonic() - started, 3)}
    finally:
        if embedding_env is not None:
            _restore_environment(previous)
    if is_error:
        raise bench.BenchError("alice_recall returned an error: " + text[:200])
    return without_echoed_query(text), status


def require_vectors_ready(run_dir: Path) -> None:
    """Refuse a search of a vectors run whose vault does not hold the vectors the build recorded.

    Every arm is held to it, not only the Alice arm, so that the answers of all arms in a run come from a run whose
    vectors were whole. A run of any other quality is not looked at. The checks are ``bench.require_vector_readiness``: nothing
    pending or stale, as many stored vectors as the manifest says, the recorded width and signature, and the
    model digest the build read. A refusal is one line and spends none of the searches.
    """

    manifest_path = run_dir / "showcase.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("search_quality") != bench.VECTORS:
        return
    settings = manifest.get("embedding_env")
    if not isinstance(settings, dict):
        raise bench.BenchError("this vectors run recorded no embedding settings; build it again with --embeddings on")
    bench.require_vector_readiness(
        bench.vault_path(run_dir / bench.VAULT_DIRNAME), manifest.get("chunk_vectors"), settings
    )


def vector_use_text(run_dir: Path) -> str:
    """How many Alice searches of a vectors run were recorded and how many ran the vector stage, by status.

    Empty for a run of any other quality, so the report of such a run is what it was.
    """

    prepared = bench.require_run_dir(run_dir)
    manifest_path = prepared / "showcase.json"
    if not manifest_path.is_file():
        return ""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("search_quality") != bench.VECTORS:
        return ""
    calls: list[dict[str, Any]] = []
    for path in sorted((prepared / bench.STATE_DIRNAME).glob(f"*/{bench.VECTOR_STATUS_FILENAME}")):
        calls.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    summary = bench.vector_stage_summary(calls)
    lines = [f"vector searches {summary['calls']}, used vectors {summary['used']}"]
    lines.extend(f"  {status}: {count}" for status, count in summary["statuses"].items())
    return "\n".join(lines) + "\n"


def _identity(db_path: Path, raw_key: str):
    from uuid import UUID

    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import resolve_agent_identity

    user_id = UUID(bench.DEFAULT_USER_ID)
    with sqlite_user_connection(db_path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        return resolve_agent_identity(store, user_id=user_id, raw_key=raw_key, payload={})


def notes_search(run_dir: Path, *, action: str, relative: str = "", pattern: str = "", options: str = "") -> str:
    prepared = bench.require_run_dir(run_dir)
    snapshot = prepared / bench.SNAPSHOT_DIRNAME
    cap = bench.load_gates(None).grep_cap
    if action == "list":
        return list_notes(snapshot, cap=cap)
    if action == "read":
        return read_note(snapshot, relative, cap=cap)
    if action == "search":
        return bench.grep_search(snapshot, pattern=pattern, options=options, cap=cap).text
    raise bench.BenchError("notes actions are list, read and search")


def _mem0_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    rows: list[Any]
    if isinstance(result, dict):
        rows = list(result.get("results") or [])
    elif isinstance(result, list):
        rows = result
    else:
        raise bench.BenchError("mem0 search returned an unexpected result")
    lines: list[str] = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
        elif isinstance(row, dict):
            memory = row.get("memory", row.get("text", ""))
            lines.append(str(memory))
    return "\n".join(lines)


def fake_separated_search(run_dir: Path, *, team: str, query: str) -> str:
    """Keyword search of the fake per-team scopes. The arm name is not mem0."""

    prepared = bench.require_run_dir(run_dir)
    payload = json.loads((prepared / "memory.json").read_text(encoding="utf-8"))
    if payload.get("engine") != "fake-separated":
        raise bench.BenchError("this run has no fake-separated index")
    check_result_label("fake-separated", "fake-separated")
    memory = FakeMemory()
    for scope, texts in payload["scopes"].items():
        for text in texts:
            memory.add(text, scope=scope)
    return "\n".join(memory.search(query, scope=team))


def mem0_search(
    run_dir: Path,
    *,
    team: str,
    query: str,
    setup: str = "separated",
    opener: Any = None,
) -> str:
    """Reopen the saved mem0 configuration and search it. A fake index is refused."""

    if setup not in {"separated", "shared"}:
        raise bench.BenchError("mem0 setup must be separated or shared")
    prepared = bench.require_run_dir(run_dir)
    payload = json.loads((prepared / "memory.json").read_text(encoding="utf-8"))
    engine = str(payload.get("engine") or "")
    if engine.startswith("fake") or "counts" in payload:
        raise bench.BenchError("a fake memory index is not mem0")
    if engine != "mem0":
        raise bench.BenchError("this run has no mem0 index")
    block = payload[setup]
    if opener is None:
        try:
            from mem0 import Memory
        except ImportError as exc:
            raise bench.BenchError("mem0 is not installed; install the bench extra") from exc
        opener = Memory.from_config
    memory = opener(block["config"])
    user_id = team if setup == "separated" else str(block["user_id"])
    return _mem0_text(memory.search(query, user_id=user_id))


def none_search() -> str:
    raise NoSearchTool("the none arm has no search tool")


def heldout_manifest(notes: Path, questions: Path) -> dict[str, str]:
    """The two paths a held-out run is given. Nothing else is looked up."""

    if not notes.is_dir():
        raise bench.BenchError("the notes folder is missing")
    if not questions.is_file():
        raise bench.BenchError("the questions file is missing")
    document = json.loads(questions.read_text(encoding="utf-8"))
    kind = require_question_kind(document)
    return {"notes": str(notes.resolve()), "questions": str(questions.resolve()), "kind": kind}


def observe_team_limits(directory: Path) -> str:
    """Run the product: import labeled notes, recall with each team key, and try memory.commit.

    The returned table is what the harness document must show. It is the run's
    results, not a copy of the permission tables in source.
    """

    import sqlite3

    from alicebot_api.vnext_agent_control import evaluate_agent_policy

    corpus = directory / "limit-notes"
    run = directory / "limit-run"

    def write(relative: str, sensitivity: str, domain: str, body: str) -> None:
        path = corpus / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "team: sales\n"
            "date: 2025-06-01\n"
            f"sensitivity: {sensitivity}\n"
            f"domain: {domain}\n"
            "kind: note\n"
            "---\n"
            "\n"
            f"{body}\n",
            encoding="utf-8",
        )

    write("public.md", "public", "professional", "The publicnote is alpha-public.")
    write("internal.md", "internal", "professional", "The internalnote is alpha-internal.")
    write("private.md", "private", "professional", "The privatenote is alpha-private.")
    write("confidential.md", "confidential", "professional", "The confidentialnote is alpha-confidential.")
    write("legal.md", "internal", "legal", "The legalnote is alpha-legal.")
    write("financial.md", "private", "financial", "The financialnote is alpha-financial.")
    import_corpus(run, corpus)
    db_path = bench.vault_path(run / bench.VAULT_DIRNAME)
    keys = _create_keys(db_path)
    (run / "keys.json").write_text(json.dumps(keys), encoding="utf-8")
    columns = (
        ("public", "publicnote", "alpha-public"),
        ("internal", "internalnote", "alpha-internal"),
        ("private", "privatenote", "alpha-private"),
        ("confidential", "confidentialnote", "alpha-confidential"),
        ("legal", "legalnote", "alpha-legal"),
        ("financial", "financialnote", "alpha-financial"),
    )
    rows: list[tuple[str, dict[str, str], str, tuple[str, ...]]] = []
    for team in TEAMS:
        seen: dict[str, str] = {}
        for name, query, sentinel in columns:
            found = alice_search(run, team=team, query=query)
            seen[name] = "yes" if sentinel in found else "no"
        identity = _identity(db_path, keys[team])
        decision = evaluate_agent_policy(identity=identity, action="memory.commit")
        rows.append((team, seen, decision.decision, tuple(decision.reasons)))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        project_rows = 0
        if "projects" in tables:
            project_rows = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
    lines = [
        "| team | profile | project binding | public | internal | private | confidential | legal | financial | memory.commit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for team, seen, commit_decision, _reasons in rows:
        lines.append(
            f"| {team} | {TEAM_PROFILES[team]} | none | {seen['public']} | {seen['internal']} | "
            f"{seen['private']} | {seen['confidential']} | {seen['legal']} | {seen['financial']} | {commit_decision} |"
        )
    support = next(seen for team, seen, _decision, _reasons in rows if team == "support")
    product = next(seen for team, seen, _decision, _reasons in rows if team == "product")
    lines.append("")
    if support == product:
        lines.append(
            "Support and product read the same notes in this run. Both keys were created with no project binding. "
            f"The vault has {project_rows} project rows, so there is no project to bind the product key to. "
            "The permission model cannot give product a different read set without that binding. "
            "The harness does not add a filter of its own."
        )
    for team, _seen, commit_decision, reasons in rows:
        if commit_decision == "blocked":
            reason_text = ", ".join(reasons) if reasons else "none"
            lines.append(
                f"The {team} key memory.commit decision was {commit_decision}. The reasons were {reason_text}. "
                "The harness does not create a project or change the profile to hide that."
            )
    return "\n".join(lines) + "\n"


def showcase_live(run_dir: Path, *, notes: int, endpoint: str) -> str:
    """Phase 0 does not run mem0. The endpoint is still checked, and a cloud host is refused.

    The mem0 search functions stay in this module. Tests do not download a model.
    """

    require_local_endpoint(endpoint)
    if notes < 1 or notes > 100:
        raise bench.BenchError("notes must be from 1 to 100")
    if not isinstance(run_dir, Path):
        raise bench.BenchError("run-dir must be a path")
    raise bench.BenchError("Phase 0 does not run the mem0 arms. They stay in the code and are not reported.")


def assert_postgres_store(database_url: str) -> None:
    """The shipped SQLite product cannot create a key and refuses a configured one."""

    if database_url.startswith("sqlite"):
        raise bench.BenchError("the shipped SQLite product cannot create a key and refuses a configured one")


def team_key_command(*, database_url: str, user_id: str, team: str, profile: str) -> list[str]:
    """``alicebot agent keys create`` for one team. The raw key is printed once by that command."""

    assert_postgres_store(database_url)
    return [
        "alicebot",
        "--database-url",
        database_url,
        "--user-id",
        user_id,
        "agent",
        "keys",
        "create",
        "--agent-id",
        f"showcase-{team}",
        "--profile",
        profile,
        "--label",
        team,
    ]


def note_import_command(
    *,
    database_url: str,
    user_id: str,
    path: str,
    domain: str,
    sensitivity: str,
) -> list[str]:
    """One note through the product's Postgres import, with that note's own labels."""

    assert_postgres_store(database_url)
    return [
        "alicebot",
        "--database-url",
        database_url,
        "--user-id",
        user_id,
        "vnext",
        "sources",
        "capture-file",
        path,
        "--domain",
        domain,
        "--sensitivity",
        sensitivity,
    ]


def http_recall_request(*, base_url: str, token: str, user_id: str, query: str) -> dict[str, Any]:
    """POST /v0/vnext/context-packs. The key is the Authorization header. sensitivity_allowed is not sent."""

    return {
        "url": base_url.rstrip("/") + KEYED_RECALL_PATH,
        "headers": {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        "body": {"user_id": user_id, "query": query},
    }


def permission_questions(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    require_question_kind(document)
    assert_phase0_report_size([document])
    rows = [item for item in document.get("questions") or [] if item.get("task") == "permission"]
    teams = {str(item.get("team")) for item in rows}
    if set(TEAMS) - teams:
        raise bench.BenchError("the permission task must cover every team")
    return rows


def keyed_section(keyed: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]]) -> str:
    """The PostgreSQL leak rate beside the SQLite harness, or a statement that the path was not run."""

    sqlite_rows = [row for row in rows if str(row.get("arm")) == "alice" and str(row.get("task")) == "permission"]
    sqlite_leaks = sum(1 for row in sqlite_rows if row.get("leak"))
    sqlite_bit = ""
    if sqlite_rows:
        sqlite_bit = f" SQLite harness permission leaks {sqlite_leaks}/{len(sqlite_rows)}."
    if not keyed or not keyed.get("ran"):
        reason = str((keyed or {}).get("reason") or "")
        if not reason:
            reason = (
                "The tower names the disposable local PostgreSQL. "
                "None is configured, and no Docker image was pulled."
            )
        return "Keyed PostgreSQL permission path: not run. " + reason + " No leak rate is reported for that path." + sqlite_bit
    return (
        f"Keyed PostgreSQL permission path: leaks {keyed['leaks']}/{keyed['total']}."
        + sqlite_bit
    )


def execute_keyed_permission(
    *,
    database_url: str,
    base_url: str,
    user_id: str,
    corpus: Path,
    document: Mapping[str, Any],
    runner: Any,
    poster: Any,
) -> dict[str, Any]:
    """Import each note, create keys with alicebot, and ask the permission task over HTTP.

    ``runner`` and ``poster`` perform the process and the HTTP call. Tests pass fakes.
    No Docker image is pulled.
    """

    assert_postgres_store(database_url)
    questions = permission_questions(document)
    from alicebot_api.markdown_import import _parse_frontmatter

    files = sorted(path for path in corpus.rglob("*.md") if path.is_file())
    if not files:
        raise bench.BenchError("the notes folder has no markdown files")
    for path in files:
        raw = path.read_text(encoding="utf-8")
        meta, _lines = _parse_frontmatter(raw)
        command = note_import_command(
            database_url=database_url,
            user_id=user_id,
            path=str(path),
            domain=str(meta.get("domain") or "professional"),
            sensitivity=str(meta.get("sensitivity") or "internal"),
        )
        runner(command)
    keys: dict[str, str] = {}
    for team, profile in TEAM_PROFILES.items():
        command = team_key_command(database_url=database_url, user_id=user_id, team=team, profile=profile)
        keys[team] = _raw_key_from_command_output(str(runner(command)))
    answers = []
    for question in questions:
        team = str(question["team"])
        request = http_recall_request(base_url=base_url, token=keys[team], user_id=user_id, query=str(question["question"]))
        if "sensitivity_allowed" in request["body"]:
            raise bench.BenchError("the keyed path does not pass sensitivity_allowed")
        text = str(poster(request))
        supplied = question.get("policy")
        if not isinstance(supplied, list):
            supplied = document.get("policy")
        answers.append(
            {
                "answer": text,
                "team": team,
                "id": question["id"],
                "policy": supplied if isinstance(supplied, list) else None,
            }
        )
    document_policy = document.get("policy")
    scored = visibility.leak_rate(
        answers,
        facts=document_policy if isinstance(document_policy, list) else None,
    )
    return {"ran": True, "leaks": scored["leaks"], "total": scored["total"], "answers": answers}


def add_showcase_commands(sub: argparse._SubParsersAction, commands: dict[str, Any]) -> None:
    """Register the showcase commands on the command table of the running module."""

    build = sub.add_parser("showcase-build", help="Import the synthetic company and make one key per team.")
    build.add_argument("--run-dir", required=True)
    build.add_argument("--size", type=int, required=True, choices=SIZES)
    build.add_argument("--seed", type=int, default=DEFAULT_SEED)
    build.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, choices=CHAT_MODELS)
    build.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    build.add_argument("--endpoint", default=DEFAULT_OLLAMA_BASE)
    build.add_argument("--embeddings", default="off", choices=("off", "on"))
    build.add_argument("--extractor", default="fake", choices=("fake", "mem0"))
    build.add_argument("--search-quality", default=None, choices=bench.SEARCH_QUALITY_CHOICES)

    questions = sub.add_parser("showcase-questions", help="Write 15 questions per task for one size.")
    questions.add_argument("--size", type=int, required=True, choices=SIZES)
    questions.add_argument("--out", required=True)

    search = sub.add_parser("showcase-search", help="One search tool for an answering agent.")
    search.add_argument("--run-dir", required=True)
    search.add_argument("--arm", required=True, choices=ARMS)
    search.add_argument("--team", default="sales")
    search.add_argument("--query", default="")
    search.add_argument("--pattern", default="")
    search.add_argument("--grep-options", default="")
    search.add_argument("--action", default="search")
    search.add_argument("--path", default="")
    search.add_argument("--state-dir", default=None)
    search.add_argument("--search-quality", default=None, choices=bench.SEARCH_QUALITY_CHOICES)
    search.add_argument("--budget", type=int, default=None)

    record = sub.add_parser(
        "showcase-record",
        help="Record one answer. A repeat of the same question, arm, team and model is kept as the first.",
    )
    record.add_argument("--run-dir", required=True)
    record.add_argument("--id", required=True)
    record.add_argument("--arm", required=True)
    record.add_argument("--team", required=True)
    record.add_argument("--answer", required=True)
    record.add_argument("--model", default=None)
    record.add_argument("--evidence", default=None, help="JSON list of objects with id and passage.")
    record.add_argument("--tokens", type=int, default=0)
    record.add_argument("--seconds", type=float, default=0.0)
    record.add_argument("--calls", type=int, default=0)
    record.add_argument("--retrieved", default=None, choices=("yes", "no"))

    packet = sub.add_parser("showcase-packet", help="Write a grading packet that does not name the arm.")
    packet.add_argument("--questions", required=True)
    packet.add_argument("--answers", required=True, help="JSON object of arm to id to answer text.")
    packet.add_argument("--out", required=True)
    packet.add_argument("--key-out", required=True)
    packet.add_argument("--seed", type=int, default=DEFAULT_SEED)

    report = sub.add_parser("showcase-report", help="Print accuracy, intervals and leak rates.")
    report.add_argument("--grades", required=True)
    report.add_argument("--run-dir", default=None)
    report.add_argument(
        "--heldout",
        action="store_true",
        help="The grades are from showcase-heldout. Omit the development-set header.",
    )

    held = sub.add_parser("showcase-heldout", help="Import a given notes folder and record the questions file it is given.")
    held.add_argument("--run-dir", required=True)
    held.add_argument("--notes", required=True)
    held.add_argument("--questions", required=True)
    held.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, choices=CHAT_MODELS)
    held.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    held.add_argument("--endpoint", default=DEFAULT_OLLAMA_BASE)
    held.add_argument("--embeddings", default="off", choices=("off", "on"))
    held.add_argument("--extractor", default="fake", choices=("fake", "mem0"))
    held.add_argument("--search-quality", default=None, choices=bench.SEARCH_QUALITY_CHOICES)

    keyed = sub.add_parser(
        "showcase-keyed-permission",
        help="Permission questions on PostgreSQL, with alicebot keys and HTTP recall.",
    )
    keyed.add_argument("--questions", required=True)
    keyed.add_argument("--database-url", default=None)
    keyed.add_argument("--base-url", default="http://127.0.0.1:8000")
    keyed.add_argument("--user-id", default=bench.DEFAULT_USER_ID)
    keyed.add_argument("--corpus", default=None)

    live = sub.add_parser(
        "showcase-live",
        help="Refuse a cloud host. Phase 0 does not run the mem0 arms.",
    )
    live.add_argument("--run-dir", required=True)
    live.add_argument("--notes", type=int, default=20)
    live.add_argument("--endpoint", default=DEFAULT_OLLAMA_BASE)

    commands.update(
        {
            "showcase-build": _cmd_build,
            "showcase-questions": _cmd_questions,
            "showcase-search": _cmd_search,
            "showcase-record": _cmd_record,
            "showcase-packet": _cmd_packet,
            "showcase-report": _cmd_report,
            "showcase-heldout": _cmd_heldout,
            "showcase-keyed-permission": _cmd_keyed_permission,
            "showcase-live": _cmd_live,
        }
    )


def _cmd_build(args: argparse.Namespace) -> int:
    manifest = build_company(
        Path(args.run_dir),
        size=args.size,
        seed=args.seed,
        chat_model=args.chat_model,
        embed_model=args.embed_model,
        endpoint=args.endpoint,
        extractor=args.extractor,
        embeddings=args.embeddings == "on",
        search_quality=args.search_quality,
    )
    print(f"built {manifest['notes']} notes, chat model {manifest['models']['chat']}, extractor {manifest['extractor']}")
    capture = manifest.get("capture")
    if isinstance(capture, dict) and capture:
        print(format_capture(capture))
    if "chunk_vectors" in manifest:
        print(bench.format_chunk_vectors(manifest["chunk_vectors"]))
    return bench.EXIT_OK


def _cmd_questions(args: argparse.Namespace) -> int:
    document = questions_for(args.size)
    Path(args.out).write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(document['questions'])} questions")
    return bench.EXIT_OK


def assert_search_quality(run_dir: Path, requested: str | None) -> None:
    """Refuse a search whose quality is not the one this run recorded."""

    manifest_path = run_dir / "showcase.json"
    recorded = "unset"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "search_quality" in manifest:
            recorded = str(manifest["search_quality"])
    asked = "unset" if requested is None else recorded_search_quality(requested)
    if asked != recorded:
        raise bench.BenchError(f"the search quality is {asked} and this run recorded {recorded}")


def recorded_budget(state: Path, requested: int | None) -> int:
    """The budget this state folder keeps. The first search records it. The default is 3."""

    path = state / "budget"
    recorded: int | None = None
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if not text.isdigit():
            raise bench.BenchError("this state folder recorded a budget that is not a number")
        recorded = int(text)
    if requested is not None and (requested < 1 or requested > 10):
        raise bench.BenchError("the search budget must be from 1 to 10")
    if requested is None:
        chosen = 3 if recorded is None else recorded
    elif recorded is not None and requested != recorded:
        raise bench.BenchError(f"this state folder recorded a budget of {recorded}")
    else:
        chosen = requested
    if recorded is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(chosen) + "\n", encoding="utf-8")
    return chosen


def _cmd_search(args: argparse.Namespace) -> int:
    if str(args.arm).startswith("mem0"):
        raise bench.BenchError("Phase 0 does not run the mem0 arms. They stay in the code and are not reported.")
    if args.arm == "none":
        none_search()
    run_dir = bench.require_run_dir(Path(args.run_dir))
    assert_search_quality(run_dir, args.search_quality)
    require_vectors_ready(run_dir)
    state = bench.resolve_inside(
        run_dir,
        args.state_dir,
        default=run_dir / bench.STATE_DIRNAME / f"{args.arm}-{args.team}",
        what="--state-dir",
    )
    budget = recorded_budget(state, args.budget)
    gates = bench.load_gates(None)

    def runner() -> bench.SearchResult:
        vector: dict[str, Any] | None = None
        if args.arm == "alice":
            text, vector = alice_search_with_status(run_dir, team=args.team, query=args.query)
        elif args.arm == "grep":
            text = bench.grep_search(
                run_dir / bench.SNAPSHOT_DIRNAME,
                pattern=args.pattern,
                options=args.grep_options,
                cap=gates.grep_cap,
            ).text
        elif args.arm == "notes":
            text = notes_search(
                run_dir,
                action=args.action,
                relative=args.path,
                pattern=args.pattern or args.query,
                options=args.grep_options,
            )
        elif args.arm == "fake-separated":
            text = fake_separated_search(run_dir, team=args.team, query=args.query)
        elif args.arm == "mem0-separated":
            text = mem0_search(run_dir, team=args.team, query=args.query, setup="separated")
        elif args.arm == "mem0-shared":
            text = mem0_search(run_dir, team=args.team, query=args.query, setup="shared")
        else:
            raise bench.BenchError("unknown arm")
        return bench.SearchResult(
            text=text, status="ok", truncated=False, raw_bytes=len(text.encode("utf-8")), vector=vector
        )

    result = bench.run_budgeted_search(
        state,
        arm=args.arm,
        search_input=args.query or args.pattern or args.action,
        budget=budget,
        runner=runner,
    )
    print(result.text)
    return bench.EXIT_OK if result.status == "ok" else bench.EXIT_SEARCH_ERROR


def recall_note_retrieved(snapshot: Path, question: Mapping[str, Any], tool_text: str) -> bool | None:
    """Whether a recall search returned each needed note.

    Other tasks return None. A needed note counts as retrieved when its last
    body line is in the tool text.
    """

    if question.get("task") != "recall":
        return None
    paths = [str(item) for item in question.get("needed_paths") or []]
    if not paths:
        return None
    folded = tool_text.casefold()
    for relative in paths:
        note = (snapshot / relative).read_text(encoding="utf-8")
        body = note.split("---", 2)[-1]
        lines = [line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")]
        if not lines or lines[-1].casefold() not in folded:
            return False
    return True


def _answer_model(run_dir: Path, explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    manifest_path = run_dir / "showcase.json"
    if not manifest_path.is_file():
        return ""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    models = manifest.get("models") or {}
    return str(models.get("chat") or "")


def _cmd_record(args: argparse.Namespace) -> int:
    run_dir = bench.require_run_dir(Path(args.run_dir))
    path = run_dir / "answers.jsonl"
    row: dict[str, Any] = {
        "id": args.id,
        "arm": args.arm,
        "team": args.team,
        "model": _answer_model(run_dir, args.model),
        "answer": args.answer,
        "tokens": args.tokens,
        "seconds": args.seconds,
        "calls": args.calls,
    }
    if args.evidence is not None:
        parsed = json.loads(args.evidence)
        if not isinstance(parsed, list):
            raise bench.BenchError("evidence is a list of ids and passages")
        row["evidence"] = parsed
    if args.retrieved is not None:
        row["retrieved"] = args.retrieved == "yes"
    wrote = record_answer(path, row)
    print("recorded" if wrote else "kept")
    return bench.EXIT_OK


def _cmd_packet(args: argparse.Namespace) -> int:
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    packet, key = grade_packet(questions, answers, seed=args.seed)
    Path(args.out).write_text(json.dumps(packet, indent=1) + "\n", encoding="utf-8")
    Path(args.key_out).write_text(json.dumps(key, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(packet['items'])} grading items")
    return bench.EXIT_OK


def assert_heldout_notes_match_run(rows: Sequence[Mapping[str, Any]], run_dir: Path) -> None:
    """A held-out grade row must carry the note count the import recorded."""

    prepared = bench.require_run_dir(run_dir)
    manifest_path = prepared / "showcase.json"
    if not manifest_path.is_file():
        raise bench.BenchError("the run does not record how many notes it imported")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = manifest.get("notes")
    if type(recorded) is not int:
        raise bench.BenchError("the run does not record how many notes it imported")
    for row in rows:
        if str(row.get("corpus") or "") != "heldout":
            continue
        notes = row.get("notes")
        if notes != recorded:
            raise bench.BenchError(
                f"a held-out grade row says {notes} notes and the import recorded {recorded}"
            )


def _cmd_report(args: argparse.Namespace) -> int:
    rows = json.loads(Path(args.grades).read_text(encoding="utf-8"))
    if args.run_dir:
        assert_heldout_notes_match_run(rows, Path(args.run_dir))
    print(render_report(rows, heldout=bool(args.heldout)), end="")
    if args.run_dir:
        print(vector_use_text(Path(args.run_dir)), end="")
    return bench.EXIT_OK


def _cmd_heldout(args: argparse.Namespace) -> int:
    notes = Path(args.notes)
    questions = Path(args.questions)
    paths = heldout_manifest(notes, questions)
    manifest = build_heldout_folder(
        Path(args.run_dir),
        notes,
        chat_model=args.chat_model,
        embed_model=args.embed_model,
        endpoint=args.endpoint,
        extractor=args.extractor,
        embeddings=args.embeddings == "on",
        search_quality=args.search_quality,
    )
    print(json.dumps(paths, sort_keys=True))
    capture = manifest.get("capture")
    if isinstance(capture, dict) and capture:
        print(format_capture(capture))
    if "chunk_vectors" in manifest:
        print(bench.format_chunk_vectors(manifest["chunk_vectors"]))
    return bench.EXIT_OK


def _cmd_keyed_permission(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    permission_questions(document)
    database_url = (
        args.database_url
        or os.environ.get("ALICE_SHOWCASE_POSTGRES_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not args.database_url and os.environ.get("DATABASE_ADMIN_URL") and not database_url:
        database_url = os.environ.get("DATABASE_ADMIN_URL")
    if not database_url:
        print(keyed_section(None, []))
        return bench.EXIT_OK
    if args.corpus is None:
        print(
            keyed_section(
                {
                    "ran": False,
                    "reason": "A corpus folder was not given. No Docker image was pulled.",
                },
                [],
            )
        )
        return bench.EXIT_OK
    result = execute_keyed_permission(
        database_url=database_url,
        base_url=args.base_url,
        user_id=args.user_id,
        corpus=Path(args.corpus),
        document=document,
        runner=_run_checked,
        poster=_post_recall,
    )
    print(keyed_section(result, []))
    return bench.EXIT_OK


def _raw_key_from_command_output(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text.strip()
    if isinstance(parsed, dict) and parsed.get("raw_key"):
        return str(parsed["raw_key"])
    return text.strip()


def _run_checked(command: Sequence[str]) -> str:
    import subprocess

    completed = subprocess.run(list(command), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise bench.BenchError("the product command failed: " + detail[:400])
    return completed.stdout


def _post_recall(request: Mapping[str, Any]) -> str:
    from urllib.request import Request, urlopen

    payload = json.dumps(request["body"]).encode("utf-8")
    headers = {str(key): str(value) for key, value in request["headers"].items()}
    outgoing = Request(str(request["url"]), data=payload, headers=headers, method="POST")
    with urlopen(outgoing, timeout=60) as response:  # noqa: S310
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def _cmd_live(args: argparse.Namespace) -> int:
    print(showcase_live(Path(args.run_dir), notes=args.notes, endpoint=args.endpoint), end="")
    return bench.EXIT_OK
