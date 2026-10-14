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
import shutil
from typing import Any
from urllib.parse import urlsplit

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
DEFAULT_EMBED_MODEL = "nomic-embed-text:v1.5"
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
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
SHOWCASE_SCHEMA = "showcase-run/1"
GRADE_SCHEMA = "showcase-grade-packet/1"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

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

    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or host not in LOOPBACK:
        raise bench.BenchError("the model endpoint must be a loopback Ollama address")
    return url.strip().rstrip("/")


def embedding_settings(base: str, model: str) -> dict[str, str]:
    """The env Alice's embedding client reads. Ollama serves ``/v1/embeddings``."""

    local = require_local_endpoint(base)
    if not model.strip():
        raise bench.BenchError("the embedding model must be set")
    return {
        "ALICE_EMBEDDINGS_BASE_URL": local + "/v1",
        "ALICE_EMBEDDINGS_MODEL": model.strip(),
    }


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


def questions_for(size: int) -> dict[str, Any]:
    """15 questions for each task, for one corpus size. About 105, and 210 across both sizes."""

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
        "permission": (
            _PERMISSION,
            {"abstain": True, "leak_strings": ["128400", "910 ledger"], "team": "support"},
        ),
    }
    if any(len(rows) != QUESTIONS_PER_TASK for rows, _rubric in stems.values()):
        raise bench.BenchError("each task needs 15 questions")
    questions = []
    for task in TASKS:
        rows, rubric = stems[task]
        for index, text in enumerate(rows, start=1):
            item = {
                "id": f"{size}-{task}-{index:02d}",
                "size": size,
                "task": task,
                "team": rubric.get("team", "sales"),
                "question": text,
                "must_include": list(rubric.get("must_include", [])),
                "must_not_include": list(rubric.get("must_not_include", [])),
                "leak_strings": list(rubric.get("leak_strings", [])),
                "abstain": bool(rubric.get("abstain", False)),
                "needed_paths": list(rubric.get("needed_paths", [])),
                "policy": visibility.policy_for_questions(),
            }
            questions.append(item)
    return {
        "schema": "showcase-questions/1",
        "company": COMPANY_NAME,
        "size": size,
        "role": "development",
        "policy": visibility.policy_for_questions(),
        "questions": questions,
    }


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


def render_report(rows: Sequence[Mapping[str, Any]], *, heldout: bool = False) -> str:
    """Accuracy and leak rate per arm and task, and where the sample cannot separate two arms.

    The questions from ``questions_for`` are a development set. A report of them
    says so. ``heldout`` is for a question set run through showcase-heldout.
    """

    for row in rows:
        engine = str(row.get("engine") or "")
        if engine:
            check_result_label(str(row["arm"]), engine)
    groups: dict[tuple[Any, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["size"], str(row["arm"]), str(row["task"])), []).append(row)
    lines = []
    by_size: dict[Any, list[tuple[str, str, tuple[float, float]]]] = {}
    for (size, arm, task), items in sorted(groups.items(), key=lambda item: (str(item[0][0]), item[0][2], item[0][1])):
        correct = sum(1 for item in items if item["correct"])
        leaks = sum(1 for item in items if item["leak"])
        low, high = wilson_interval(correct, len(items))
        lines.append(
            f"size {size} arm {arm} task {task}: {correct}/{len(items)} "
            f"interval {low:.2f} to {high:.2f}; leaks {leaks}/{len(items)}"
        )
        by_size.setdefault(size, []).append((arm, task, (low, high)))
    for size, entries in by_size.items():
        for index, (arm, task, interval) in enumerate(entries):
            for other_arm, other_task, other in entries[index + 1 :]:
                if task == other_task and arm != other_arm and intervals_overlap(interval, other):
                    lines.append(
                        f"size {size} task {task}: {arm} and {other_arm} overlap, "
                        "so this sample cannot separate them"
                    )
    arms = {str(row["arm"]) for row in rows}
    if "mem0-separated" in arms:
        lines.append(MEM0_SEPARATED_LINE)
    if "mem0-shared" in arms:
        lines.append(MEM0_SHARED_LINE)
    body = "\n".join(lines) + ("\n" if lines else "")
    if heldout:
        return body
    return DEVELOPMENT_HEADER + body


def answer_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """An answer is one question, one arm and one team."""

    if "arm" not in row or "team" not in row:
        raise bench.BenchError("an answer needs an arm and a team")
    return (str(row["id"]), str(row["arm"]), str(row["team"]))


def check_result_label(arm: str, engine: str) -> None:
    """A keyword stand-in is not mem0, so it cannot be recorded under a mem0 name."""

    if engine.startswith("fake") and "mem0" in arm:
        raise bench.BenchError("a fake memory result cannot be labeled mem0")


def load_done(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not path.is_file():
        return {}
    done: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done.setdefault(answer_identity(row), row)
    return done


def record_answer(path: Path, row: Mapping[str, Any]) -> bool:
    """Append one answer. A second write of the same question, arm and team stays the first."""

    if "engine" in row:
        check_result_label(str(row["arm"]), str(row["engine"]))
    done = load_done(path)
    if answer_identity(row) in done:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), sort_keys=True) + "\n")
    return True


def pending(
    questions: Sequence[Mapping[str, Any]],
    done: Mapping[Any, Any],
    limit: int,
    *,
    arm: str,
    team: str,
) -> list[Mapping[str, Any]]:
    waiting = [question for question in questions if (question["id"], arm, team) not in done]
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
) -> dict[str, Any]:
    """Import the company with each note's own labels and make one key per team."""

    local = require_local_endpoint(endpoint)
    if chat_model not in CHAT_MODELS:
        raise bench.BenchError("the chat model must be qwen2.5:7b, llama3.2, or gemma4")
    notes = generate(size, seed=seed)
    prepared = bench.prepare_run_dir(run_dir, create=True)
    corpus = prepared / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    write_notes(notes, corpus)
    snapshot = prepared / bench.SNAPSHOT_DIRNAME
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(corpus, snapshot)
    embed = embedding_settings(local, embed_model) if embeddings else None
    db_path = import_corpus(prepared, corpus, embeddings=embed)
    keys = _create_keys(db_path)
    (prepared / "keys.json").write_text(json.dumps(keys, indent=1) + "\n", encoding="utf-8")
    memory = _index_memory(notes, extractor=extractor, chat_model=chat_model, embed_model=embed_model, endpoint=local, store_path=prepared / "mem0")
    (prepared / "memory.json").write_text(json.dumps(memory, indent=1) + "\n", encoding="utf-8")
    manifest = {
        "schema": SHOWCASE_SCHEMA,
        "company": COMPANY_NAME,
        "size": size,
        "seed": seed,
        "notes": len(notes),
        "models": {"chat": chat_model, "embed": embed_model, "endpoint": local},
        "embedding_env": embedding_settings(local, embed_model),
        "embeddings": "on" if embeddings else "off",
        "extractor": extractor,
        "teams": dict(TEAM_PROFILES),
    }
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


def embed_note_texts(texts: Sequence[str], settings: Mapping[str, str]) -> int:
    """Send note text to the configured loopback embeddings endpoint.

    Markdown import does not embed. This is the call that uses the endpoint.
    """

    from alicebot_api.vnext_embeddings import MAX_EMBEDDINGS_BATCH_SIZE, get_embedding_provider

    pending_texts = [text for text in texts if text.strip()]
    if not pending_texts:
        raise bench.BenchError("there is no note text to embed")
    previous = _apply_embedding_settings(settings)
    try:
        provider = get_embedding_provider()
        if provider is None:
            raise bench.BenchError("the embedding provider is not configured")
        sent = 0
        for start in range(0, len(pending_texts), MAX_EMBEDDINGS_BATCH_SIZE):
            batch = pending_texts[start : start + MAX_EMBEDDINGS_BATCH_SIZE]
            vectors = provider.embed_batch(batch)
            if len(vectors) != len(batch):
                raise bench.BenchError("the embedding endpoint returned the wrong number of vectors")
            sent += len(vectors)
        return sent
    finally:
        _restore_environment(previous)


def import_corpus(run_dir: Path, corpus: Path, *, embeddings: Mapping[str, str] | None = None) -> Path:
    """Import each note with the domain and sensitivity in its front matter.

    The vault directory belongs to the harness. It is removed on every import,
    so a note that left the folder cannot stay in the vault.
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
    for path in files:
        raw = path.read_text(encoding="utf-8")
        texts.append(raw)
        meta, _lines = _parse_frontmatter(raw)
        bench._import_one(
            db_path,
            path,
            domain=meta.get("domain", "professional"),
            sensitivity=meta.get("sensitivity", "internal"),
        )
    if embeddings is not None:
        embed_note_texts(texts, embeddings)
    return db_path


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


def alice_search(run_dir: Path, *, team: str, query: str) -> str:
    if team not in TEAM_PROFILES:
        raise bench.BenchError("unknown team")
    from alicebot_api.vnext_agent_control import _profile_sensitivity

    prepared = bench.require_run_dir(run_dir)
    keys = json.loads((prepared / "keys.json").read_text(encoding="utf-8"))
    db_path = bench.vault_path(prepared / bench.VAULT_DIRNAME)
    identity = _identity(db_path, keys[team])
    manifest_path = prepared / "showcase.json"
    embedding_env: dict[str, str] | None = None
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("embeddings") == "on":
            embedding_env = dict(manifest["embedding_env"])
    previous: dict[str, str | None] = {}
    if embedding_env is not None:
        embed_note_texts([query], embedding_env)
        previous = _apply_embedding_settings(embedding_env)
    is_error = False
    text = ""
    try:
        session = bench.McpSession(db_path, agent_identity=identity)
        arguments = bench.alice_arm_arguments(query)
        arguments["sensitivity_allowed"] = list(_profile_sensitivity(TEAM_PROFILES[team]))
        is_error, text = session.call(bench.RECALL_TOOL, arguments)
    finally:
        if embedding_env is not None:
            _restore_environment(previous)
    if is_error:
        raise bench.BenchError("alice_recall returned an error: " + text[:200])
    return text


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
    return {"notes": str(notes.resolve()), "questions": str(questions.resolve())}


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
    """Build a small company, the real mem0 arm and the Alice arm, then search once each.

    The endpoint has to be loopback. This is the command to run on a machine
    that already has Ollama. Tests do not download a model.
    """

    local = require_local_endpoint(endpoint)
    if notes < 1 or notes > 100:
        raise bench.BenchError("notes must be from 1 to 100")
    selected = list(generate(1000)[:notes])
    prepared = bench.prepare_run_dir(run_dir, create=True)
    corpus = prepared / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    write_notes(tuple(selected), corpus)
    snapshot = prepared / bench.SNAPSHOT_DIRNAME
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(corpus, snapshot)
    settings = embedding_settings(local, DEFAULT_EMBED_MODEL)
    db_path = import_corpus(prepared, corpus, embeddings=settings)
    keys = _create_keys(db_path)
    (prepared / "keys.json").write_text(json.dumps(keys) + "\n", encoding="utf-8")
    memory = _index_memory(
        selected,
        extractor="mem0",
        chat_model=DEFAULT_CHAT_MODEL,
        embed_model=DEFAULT_EMBED_MODEL,
        endpoint=local,
        store_path=prepared / "mem0",
    )
    (prepared / "memory.json").write_text(json.dumps(memory) + "\n", encoding="utf-8")
    manifest = {
        "schema": SHOWCASE_SCHEMA,
        "embeddings": "on",
        "embedding_env": settings,
        "notes": len(selected),
        "extractor": "mem0",
        "teams": dict(TEAM_PROFILES),
    }
    (prepared / "showcase.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    words = [word for word in selected[0].body.split() if len(word) > 4]
    query = words[0] if words else "note"
    alice_text = alice_search(prepared, team="leadership", query=query)
    mem0_text = mem0_search(prepared, team="leadership", query=query, setup="separated")
    return f"alice\n{alice_text}\nmem0-separated\n{mem0_text}\n"


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

    record = sub.add_parser(
        "showcase-record",
        help="Record one answer. A repeat of the same question, arm and team is kept as the first.",
    )
    record.add_argument("--run-dir", required=True)
    record.add_argument("--id", required=True)
    record.add_argument("--arm", required=True)
    record.add_argument("--team", required=True)
    record.add_argument("--answer", required=True)
    record.add_argument("--tokens", type=int, default=0)
    record.add_argument("--seconds", type=float, default=0.0)
    record.add_argument("--calls", type=int, default=0)

    packet = sub.add_parser("showcase-packet", help="Write a grading packet that does not name the arm.")
    packet.add_argument("--questions", required=True)
    packet.add_argument("--answers", required=True, help="JSON object of arm to id to answer text.")
    packet.add_argument("--out", required=True)
    packet.add_argument("--key-out", required=True)
    packet.add_argument("--seed", type=int, default=DEFAULT_SEED)

    report = sub.add_parser("showcase-report", help="Print accuracy, intervals and leak rates.")
    report.add_argument("--grades", required=True)
    report.add_argument(
        "--heldout",
        action="store_true",
        help="The grades are from showcase-heldout. Omit the development-set header.",
    )

    held = sub.add_parser("showcase-heldout", help="Record a notes folder and a questions file the bench has not seen.")
    held.add_argument("--notes", required=True)
    held.add_argument("--questions", required=True)

    live = sub.add_parser("showcase-live", help="Build 20 notes through loopback Ollama and search once with Alice and mem0.")
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
    )
    print(f"built {manifest['notes']} notes, chat model {manifest['models']['chat']}, extractor {manifest['extractor']}")
    return bench.EXIT_OK


def _cmd_questions(args: argparse.Namespace) -> int:
    document = questions_for(args.size)
    Path(args.out).write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(document['questions'])} questions")
    return bench.EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    if args.arm == "none":
        none_search()
    run_dir = bench.require_run_dir(Path(args.run_dir))
    state = bench.resolve_inside(
        run_dir,
        args.state_dir,
        default=run_dir / bench.STATE_DIRNAME / f"{args.arm}-{args.team}",
        what="--state-dir",
    )
    gates = bench.load_gates(None)

    def runner() -> bench.SearchResult:
        if args.arm == "alice":
            text = alice_search(run_dir, team=args.team, query=args.query)
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
        return bench.SearchResult(text=text, status="ok", truncated=False, raw_bytes=len(text.encode("utf-8")))

    result = bench.run_budgeted_search(
        state,
        arm=args.arm,
        search_input=args.query or args.pattern or args.action,
        budget=gates.searches_per_run,
        runner=runner,
    )
    print(result.text)
    return bench.EXIT_OK


def _cmd_record(args: argparse.Namespace) -> int:
    path = bench.require_run_dir(Path(args.run_dir)) / "answers.jsonl"
    wrote = record_answer(
        path,
        {
            "id": args.id,
            "arm": args.arm,
            "team": args.team,
            "answer": args.answer,
            "tokens": args.tokens,
            "seconds": args.seconds,
            "calls": args.calls,
        },
    )
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


def _cmd_report(args: argparse.Namespace) -> int:
    rows = json.loads(Path(args.grades).read_text(encoding="utf-8"))
    print(render_report(rows, heldout=bool(args.heldout)), end="")
    return bench.EXIT_OK


def _cmd_heldout(args: argparse.Namespace) -> int:
    print(json.dumps(heldout_manifest(Path(args.notes), Path(args.questions)), sort_keys=True))
    return bench.EXIT_OK


def _cmd_live(args: argparse.Namespace) -> int:
    print(showcase_live(Path(args.run_dir), notes=args.notes, endpoint=args.endpoint), end="")
    return bench.EXIT_OK
