"""Tier 1 scoring (scripts/alice_bench.py): what counts as text from the vault, and how it is counted.

Tier 1 asks whether the facts a correct answer needs are present in what the agent would
read. It scores text that came out of the vault and nothing else, it counts the whole
serialized output against the byte budget, and it prints a negative control beside every
table. These tests use the invented fixture corpus and hand-made recall outputs. They need
no vault, no model and no network.

Every test names the mutation that must fail it. Test ids (TH4, TH5, ...) follow the spec of
the search-quality release.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.alice_bench as bench
from alicebot_api.mcp.retrieval import _compact_recall_result
from alicebot_api.recall_framing import serialize_mcp_tool_result
from alicebot_api.session_briefing import SESSION_BRIEF_FRAME

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "search_quality"
QUESTIONS = FIXTURES / "questions.json"
OUTPUTS = FIXTURES / "outputs.json"
CORPUS = FIXTURES / "corpus"
WRITER = {"id": "owner", "established": "declared_on_keyless_install"}


def _quote(text: str) -> str:
    """How recall quotes stored text: flatten whitespace, then JSON-quote."""

    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def _source(*, excerpt: str, source_id: str = "s1", title: str = "note", captured_at: str = "2026-10-02T10:00:00Z") -> dict[str, Any]:
    return {
        "id": source_id,
        "title": _quote(title),
        "captured_at": captured_at,
        "excerpt": _quote(excerpt),
        "excerpt_kind": "imported_source_material",
        "source_type": "markdown",
        "domain": "project",
        "sensitivity": "internal",
        "writer": dict(WRITER),
    }


def _output(
    *,
    query: str = "a question",
    sources: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "framing": SESSION_BRIEF_FRAME,
        "query": query,
        "results": results or [],
        "count": len(results or []),
    }
    if sources:
        payload["sources"] = sources
        payload["source_count"] = len(sources)
    if entities:
        payload["entities"] = entities
    payload.update(extra or {})
    return serialize_mcp_tool_result(payload)


def _fact(*anchors: str) -> bench.Fact:
    return bench.Fact(label="f", anchors=tuple(bench.Anchor(text=text, file="x.md") for text in anchors))


def _gates() -> bench.Gates:
    return bench.load_gates()


# TH4 ---------------------------------------------------------------------

ANCHOR = "violet lantern 4821"


@pytest.mark.parametrize(
    "placement",
    ["query", "title", "captured_at", "entity_name", "source_id", "result_id", "excerpt_kind_is_not_text"],
)
def test_text_that_is_not_vault_text_never_scores(placement: str) -> None:
    """TH4. The echoed question, a title, a capture date, an entity name and an id never score.

    Mutation: score every string leaf (drop the ``leaf.path in SCORED_LEAVES`` test in
    ``score_output``). A question that repeats an anchor, or a date anchor that matches the build
    day, would then score with no retrieval at all.
    """

    source = _source(excerpt="nothing relevant here")
    entities: list[dict[str, Any]] | None = None
    results: list[dict[str, Any]] | None = None
    query = "a question"
    if placement == "query":
        query = f"please find the {ANCHOR} for me"
    elif placement == "title":
        source = _source(excerpt="nothing relevant here", title=f"about the {ANCHOR}")
    elif placement == "captured_at":
        source = _source(excerpt="nothing relevant here", captured_at=ANCHOR)
    elif placement == "entity_name":
        entities = [{"entity_type": "other", "id": "e1", "mention_count": 1, "name": ANCHOR}]
    elif placement == "source_id":
        source = _source(excerpt="nothing relevant here", source_id=ANCHOR)
    elif placement == "result_id":
        results = [{"id": ANCHOR, "text": _quote("also nothing"), "type": "project_fact", "writer": dict(WRITER)}]
    text = _output(query=query, sources=[source], results=results, entities=entities)
    assert bench.score_output(text, [_fact(ANCHOR)], budget=8192).found == (False,)


@pytest.mark.parametrize("where", ["source_excerpt", "result_text"])
def test_text_the_vault_returned_scores(where: str) -> None:
    """TH4. The two scored leaves do score: ``sources[].excerpt`` and ``results[].text``.

    Mutation: drop one of them from ``SCORED_LEAVES``.
    """

    if where == "source_excerpt":
        text = _output(sources=[_source(excerpt=f"the {ANCHOR} is in the shed")])
    else:
        text = _output(results=[{"id": "m1", "text": _quote(f"the {ANCHOR} is in the shed"), "writer": dict(WRITER)}])
    assert bench.score_output(text, [_fact(ANCHOR)], budget=8192).found == (True,)
    assert bench.SCORED_LEAVES == {"sources[].excerpt", "results[].text"}
    assert set(_gates().data["tier1"]["scored_leaves"]) == set(bench.SCORED_LEAVES)


def test_every_string_leaf_of_a_real_output_is_classified_and_a_new_key_fails_until_it_is() -> None:
    """TH4. The scorer fails closed: a leaf that is neither scored nor known not to be stops it.

    Mutation: let ``classify_leaves`` ignore unknown leaves, or add the new key below to
    ``UNSCORED_LEAVES`` without deciding. The committed outputs hold every leaf the product
    emits today, so a new field in the product shows up here before it shows up in a run.
    """

    document = json.loads(OUTPUTS.read_text())
    seen: set[str] = set()
    for per_variant in document["outputs"].values():
        for text in per_variant.values():
            leaves = bench.locate_leaves(text)
            bench.classify_leaves(leaves)
            seen.update(leaf.path for leaf in leaves)
    assert {"sources[].excerpt", "sources[].title", "framing", "query", "sources[].writer.id"} <= seen
    assert seen <= bench.SCORED_LEAVES | bench.UNSCORED_LEAVES
    assert not bench.SCORED_LEAVES & bench.UNSCORED_LEAVES

    planted = _output(sources=[{**_source(excerpt="x"), "notes": "a field nobody decided about"}])
    with pytest.raises(bench.UnclassifiedLeafError, match=r"sources\[\]\.notes"):
        bench.score_output(planted, [_fact("x")], budget=8192)
    debug = _output(sources=[_source(excerpt="x")], extra={"retrieval": {"context_depth": "low"}})
    with pytest.raises(bench.UnclassifiedLeafError, match="retrieval"):
        bench.score_output(debug, [_fact("x")], budget=8192)


def test_the_products_own_compact_fields_are_all_classified() -> None:
    """TH4. A field added to the product's source or result shape fails here until it is decided.

    Mutation: add a string field to ``_COMPACT_SOURCE_FIELDS`` or to ``_compact_recall_result``
    without adding it to the scorer's lists.
    """

    from alicebot_api.mcp.context import _COMPACT_SOURCE_FIELDS

    non_string_source_fields = {"derived_memory_corrected"}
    for field in _COMPACT_SOURCE_FIELDS:
        if field in non_string_source_fields:
            continue
        path = f"sources[].{field}"
        assert path in bench.SCORED_LEAVES | bench.UNSCORED_LEAVES, field
    compact = _compact_recall_result(
        {
            "id": "m",
            "memory_type": "project_fact",
            "canonical_text": "t",
            "domain": "project",
            "status": "active",
            "confidence": 0.9,
        },
        score=0.5,
        provenance_count=1,
    )
    for key, value in compact.items():
        if isinstance(value, str):
            assert f"results[].{key}" in bench.SCORED_LEAVES | bench.UNSCORED_LEAVES, key


def test_the_byte_budget_counts_the_whole_serialized_output_and_not_just_scored_text() -> None:
    """TH4. An entities list ahead of ``sources`` spends the budget, because the agent pays for it.

    Mutation: count only scored strings toward the budget (measure the excerpt's offset among the
    scored strings instead of in the serialized output).
    """

    entities = [
        {"entity_type": "other", "id": f"e{number}", "mention_count": 1, "name": f"Invented Person {number:03d}"}
        for number in range(70)
    ]
    text = _output(sources=[_source(excerpt=f"the {ANCHOR} is in the shed")], entities=entities)
    assert 4096 < text.index("violet lantern") < 8192
    low = bench.score_output(text, [_fact(ANCHOR)], budget=4096)
    high = bench.score_output(text, [_fact(ANCHOR)], budget=8192)
    assert (low.found, high.found) == ((False,), (True,))
    assert high.bytes_total == len(text.encode("utf-8"))
    assert (high.documents, high.passages) == (1, 1)


def test_documents_per_call_counts_distinct_ids_and_passages_count_entries() -> None:
    """The report ties relevance to context cost: documents and passages per call.

    Mutation: count entries as documents (one id repeated by a passage-mode source list).
    """

    text = _output(
        sources=[
            _source(excerpt="first part", source_id="same"),
            _source(excerpt="second part", source_id="same"),
            _source(excerpt="other", source_id="other"),
        ]
    )
    result = bench.score_output(text, [_fact("second part")], budget=8192)
    assert (result.documents, result.passages) == (2, 3)
    assert result.found == (True,)


def test_an_anchor_with_a_quote_a_backslash_and_a_non_ascii_letter_scores() -> None:
    """The scorer reads the quoting layer off an excerpt before it compares.

    Mutation: compare the leaf as it arrives (skip ``unquote_leaf``). Recall escapes a quote and a
    backslash inside the excerpt, so these anchors would never match.
    """

    body = 'The keeper writes "checked by telephone" and files it under C:\\logs at the café.'
    text = _output(sources=[_source(excerpt=body)])
    facts = [
        _fact('writes "checked by telephone" and files it'),
        _fact("under C:\\logs at the café"),
        _fact("checked   by\ntelephone"),
    ]
    assert bench.score_output(text, facts, budget=8192).found == (True, True, True)
    assert bench.unquote_leaf('"plain"') == "plain"
    assert bench.unquote_leaf("no quotes") == "no quotes"
    assert bench.unquote_leaf('"unclosed') == '"unclosed'


def test_locate_leaves_refuses_text_that_is_not_the_servers_compact_json() -> None:
    """TH4. An offset can only come from text that has the serializer's shape.

    Mutation: skip the comparison of the re-emitted text with the original in ``locate_leaves``.
    """

    pretty = json.dumps(json.loads(_output(sources=[_source(excerpt="x")])), indent=1)
    with pytest.raises(bench.BenchError, match="compact JSON"):
        bench.locate_leaves(pretty)


# TH5 ---------------------------------------------------------------------


def _write_questions(path: Path, questions: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema": bench.QUESTIONS_SCHEMA, "set_id": "t", "questions": questions}), encoding="utf-8")
    return path


def _q(qid: str, question: str, anchors: list[tuple[str, str]], keyword_query: str | None = "kw") -> dict[str, Any]:
    return {
        "id": qid,
        "question": question,
        "keyword_query": keyword_query,
        "facts": [{"fact": "f", "anchors": [{"text": text, "file": name} for name, text in anchors]}],
    }


SNAPSHOT = {
    "a.md": "The ferry leaves at nine on Tuesdays. The ferry is blue.",
    "b.md": "Dock fees are paid in cash. The ferry is blue.",
    "c.md": "Nothing about boats here. The ferry is blue.",
    "d.md": "Another page. The ferry is blue.",
}


def test_an_anchor_that_is_not_verbatim_in_its_named_file_fails_the_build(tmp_path: Path) -> None:
    """TH5. A corpus change or a typo cannot pass quietly.

    Mutation: skip the verbatim check in ``verify_anchors``. A typo, a wrong file name or a text that
    only occurs in another file would then pass.
    """

    typo = _write_questions(tmp_path / "typo.json", [_q("q1", "when?", [("a.md", "The ferry leaves at ten on Tuesdays")])])
    with pytest.raises(bench.AnchorError, match=r"q1 fact 1 in a\.md: not verbatim"):
        bench.verify_anchors(bench.load_questions(typo), SNAPSHOT, _gates())
    wrong_file = _write_questions(tmp_path / "file.json", [_q("q2", "when?", [("b.md", "The ferry leaves at nine on Tuesdays")])])
    with pytest.raises(bench.AnchorError, match=r"q2 fact 1 in b\.md: not verbatim"):
        bench.verify_anchors(bench.load_questions(wrong_file), SNAPSHOT, _gates())
    missing_file = _write_questions(tmp_path / "gone.json", [_q("q3", "when?", [("zz.md", "The ferry")])])
    with pytest.raises(bench.AnchorError, match="no such file"):
        bench.verify_anchors(bench.load_questions(missing_file), SNAPSHOT, _gates())
    good = _write_questions(
        tmp_path / "good.json", [_q("q4", "when?", [("a.md", "The ferry leaves at   nine\non Tuesdays")])]
    )
    report = bench.verify_anchors(bench.load_questions(good), SNAPSHOT, _gates())
    assert report["anchors"] == 1 and report["flagged"] == 0

    run = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--questions", str(QUESTIONS)]) == 0
    broken = _write_questions(tmp_path / "broken.json", [_q("q5", "who?", [("tide-keeping.md", "Marta Quill holds both keys")])])
    assert bench.main(["anchors", "--run-dir", str(run), "--questions", str(broken)]) == bench.EXIT_REFUSED


def test_anchors_that_are_short_common_or_in_their_question_are_flagged(tmp_path: Path) -> None:
    """TH5. Short (under 12 characters), in more than 3 files, or present in the question's own text.

    Mutation: skip the question-occurrence flag (or the length or file-count flag) in
    ``verify_anchors``. An anchor that the question repeats would then score as a free hit.
    """

    questions = [
        _q("short", "when?", [("a.md", "on Tuesdays")]),
        _q("common", "what colour?", [("a.md", "The ferry is blue")]),
        _q("echoed", "Does the ferry leave at nine on Tuesdays?", [("a.md", "The ferry leaves at nine on Tuesdays")]),
        _q("echoed_keyword", "when?", [("b.md", "Dock fees are paid in cash")], keyword_query="Dock fees are paid in cash today"),
        _q("clean", "when does it go?", [("a.md", "The ferry leaves at nine")]),
    ]
    questions[2]["question"] = "Is it true that The ferry leaves at nine on Tuesdays?"
    path = _write_questions(tmp_path / "flags.json", questions)
    report = bench.verify_anchors(bench.load_questions(path), SNAPSHOT, _gates())
    flags = {row["question"]: row["flags"] for row in report["rows"]}
    assert flags["short"] == ["short"]
    assert flags["common"] == ["many_files"]
    assert flags["echoed"] == ["in_question"]
    assert flags["echoed_keyword"] == ["in_question"]
    assert flags["clean"] == []
    assert report["flagged"] == 4 and report["flagged_unapproved"] == 4

    approved = _q("short", "when?", [("a.md", "on Tuesdays")])
    approved["facts"][0]["anchors"][0]["approved"] = "a day name is the fact"
    path = _write_questions(tmp_path / "approved.json", [approved])
    report = bench.verify_anchors(bench.load_questions(path), SNAPSHOT, _gates())
    assert (report["flagged"], report["flagged_unapproved"]) == (1, 0)


def test_the_build_can_refuse_flagged_anchors_that_nobody_approved(tmp_path: Path) -> None:
    """TH5. ``--strict-flags`` turns an unapproved flag into a failed build.

    Mutation: ignore ``strict`` in ``_print_anchor_report``.
    """

    run = tmp_path / "run"
    flagged = _write_questions(tmp_path / "f.json", [_q("q", "what?", [("tide-keeping.md", "ledger")])])
    assert bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--questions", str(flagged)]) == 0
    assert bench.main(["anchors", "--run-dir", str(run), "--questions", str(flagged), "--strict-flags"]) == bench.EXIT_REFUSED


def test_the_fixture_questions_are_clean_against_the_fixture_corpus(tmp_path: Path) -> None:
    """The public fixture is a worked example: every anchor verbatim and none flagged.

    Mutation: edit one fixture anchor so it is not in its file.
    """

    run = tmp_path / "run"
    assert bench.main(["build", "--run-dir", str(run), "--corpus", str(CORPUS)]) == 0
    report = bench.verify_anchors(bench.load_questions(QUESTIONS), bench.read_snapshot(run / bench.SNAPSHOT_DIRNAME), _gates())
    assert report["flagged"] == 0 and report["anchors"] == 19


# TH6 ---------------------------------------------------------------------


def _two_question_set(tmp_path: Path) -> bench.QuestionSet:
    path = _write_questions(
        tmp_path / "two.json",
        [
            _q("q1", f"Where is the {ANCHOR}?", [("x.md", ANCHOR)]),
            _q("q2", "Where is the ladder?", [("x.md", "the ladder is in the shed")]),
        ],
    )
    return bench.load_questions(path)


def test_the_negative_control_scores_each_questions_anchors_against_the_next_output(tmp_path: Path) -> None:
    """TH6. The floor is the hit rate of a question's anchors against another question's output.

    Mutation: skip the rotation (score a question against its own output, which makes the floor
    equal the hit rate), or never compute it.
    """

    qset = _two_question_set(tmp_path)
    own = _output(sources=[_source(excerpt=f"the {ANCHOR} sits on the sill")])
    other = _output(sources=[_source(excerpt="the ladder is in the shed")])
    clean = bench.score_set({"q1": {"verbatim": own}, "q2": {"verbatim": other}}, qset, variant="verbatim", budget=8192)
    assert (clean["hits"], clean["floor_hits"]) == (2, 0)
    leaky = bench.score_set({"q1": {"verbatim": own}, "q2": {"verbatim": own}}, qset, variant="verbatim", budget=8192)
    assert leaky["hits"] == 1
    assert leaky["floor_hits"] == 1, "q2's output holds q1's anchor, so q1 scores against it through the rotation"
    single = bench.load_questions(_write_questions(tmp_path / "one.json", [_q("q1", "x?", [("x.md", ANCHOR)])]))
    assert bench.score_set({"q1": {"verbatim": own}}, single, variant="verbatim", budget=8192)["floor_hits"] is None


def test_an_anchor_planted_in_a_questions_own_text_scores_zero(tmp_path: Path) -> None:
    """TH6. The echoed query carries the anchor and nothing scores.

    Mutation: score the ``query`` leaf. Both the hit and the floor would turn into 1.
    """

    qset = _two_question_set(tmp_path)
    echoing = _output(query=f"Where is the {ANCHOR}?", sources=[_source(excerpt="nothing relevant")])
    assert bench.score_output(echoing, qset.questions[0].facts, budget=8192).found == (False,)
    result = bench.score_set(
        {"q1": {"verbatim": echoing}, "q2": {"verbatim": echoing}}, qset, variant="verbatim", budget=8192
    )
    assert (result["hits"], result["floor_hits"]) == (0, 0)


def test_the_report_prints_the_floor_beside_every_table_and_the_minimum_over_orders(tmp_path: Path) -> None:
    """TH6 and TH14. The dev report names each import order and the lowest hit count over the orders.

    Mutation: leave the floor column out of ``format_report``, or take the maximum (or the first
    order) instead of the minimum in ``minimum_across_orders``.
    """

    qset = bench.load_questions(QUESTIONS)
    outputs = json.loads(OUTPUTS.read_text())["outputs"]
    ids = [question.id for question in qset.questions]
    rotated = {qid: outputs[ids[(index + 1) % len(ids)]] for index, qid in enumerate(ids)}
    half = {qid: (outputs[qid] if index % 2 == 0 else rotated[qid]) for index, qid in enumerate(ids)}
    paths = []
    for label, body in (("sorted", outputs), ("reverse", rotated), ("shuffle:3", half)):
        path = tmp_path / f"{label.replace(':', '-')}.json"
        path.write_text(
            json.dumps({"schema": bench.OUTPUTS_SCHEMA, "fingerprint": {"import_order": label}, "outputs": body}),
            encoding="utf-8",
        )
        paths.append(str(path))
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert bench.main(["score", "--questions", str(QUESTIONS), "--outputs", *paths]) == 0
    printed = stdout.getvalue()
    for label in ("order sorted", "order reverse", "order shuffle:3", "floor", "minimum hits over the import orders"):
        assert label in printed
    assert "gates.json sha256" in printed
    reports = [
        (label, bench.score_outputs_document(json.loads(Path(path).read_text()), qset, _gates()))
        for label, path in zip(("sorted", "reverse", "shuffle:3"), paths)
    ]
    hits = [report["variants"]["verbatim"]["8192"]["hits"] for _, report in reports]
    assert hits == [12, 0, 6]
    minimums = bench.minimum_across_orders(reports, _gates())
    assert minimums["verbatim"]["8192"] == min(hits)
    assert f"verbatim: 4096 bytes {minimums['verbatim']['4096']}, 8192 bytes {minimums['verbatim']['8192']}" in printed


# TH12 --------------------------------------------------------------------


def _reference_scored_strings(text: str) -> list[tuple[str, int]]:
    """A second implementation of "scored strings and where they end", by another method.

    The scorer in ``alice_bench`` re-emits the parsed JSON and counts bytes as it writes. This one
    walks the parsed value for the two scored keys and finds each string literal in the text with
    a moving cursor. Both must agree on what a scored string is and where it ends.
    """

    parsed = json.loads(text)
    cursor = 0
    found: list[tuple[str, int]] = []

    def visit(node: object, path: tuple[str, ...]) -> None:
        nonlocal cursor
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, (*path, key))
        elif isinstance(node, list):
            for child in node:
                visit(child, (*path, "[]"))
        elif isinstance(node, str):
            literal = json.dumps(node)
            start = text.index(literal, cursor)
            cursor = start + len(literal)
            label = ".".join(path).replace(".[]", "[]")
            if label in ("sources[].excerpt", "results[].text"):
                found.append((node, len(text[:cursor].encode("utf-8"))))

    visit(parsed, ())
    return found


def _reference_hit_flags(text: str, facts: list[bench.Fact], budget: int) -> tuple[bool, ...]:
    def squash(value: str) -> str:
        return " ".join(value.split())

    def unquote(value: str) -> str:
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            return decoded if isinstance(decoded, str) else value
        return value

    readable = [squash(unquote(value)) for value, end in _reference_scored_strings(text) if end <= budget]
    return tuple(
        any(squash(anchor.text) in body for anchor in fact.anchors for body in readable) for fact in facts
    )


def test_the_repo_scorer_and_an_independent_scorer_agree_on_committed_outputs(tmp_path: Path) -> None:
    """TH12. Two implementations, one answer, on outputs that sit in the repository.

    The repository scorer and the stdlib scorer the tower keeps are separate programs, and a
    reviewer reading a scorer that "agrees" has no evidence. This runs the repository scorer against
    a second implementation (above) on the committed outputs, on the same outputs with each question
    scored against its neighbour's, and on hand-made edge cases, at several budgets.

    Mutation: change a normalisation rule in one scorer only, for example drop the whitespace
    collapse from ``normalize_ws`` or skip ``unquote_leaf``.
    """

    qset = bench.load_questions(QUESTIONS)
    outputs = json.loads(OUTPUTS.read_text())["outputs"]
    ids = [question.id for question in qset.questions]
    cases: list[tuple[str, list[bench.Fact]]] = []
    for index, question in enumerate(qset.questions):
        for variant in bench.VARIANTS:
            cases.append((outputs[question.id][variant], list(question.facts)))
            neighbour = outputs[ids[(index + 1) % len(ids)]][variant]
            cases.append((neighbour, list(question.facts)))
    edge = _output(sources=[_source(excerpt='say "hi"\tto   the café   crew, C:\\bin')])
    cases.append(
        (
            edge,
            [_fact('say "hi" to the café crew'), _fact("C:\\bin"), _fact("say  \n \"hi\""), _fact("not in the output")],
        )
    )
    compared = 0
    hits_seen: set[bool] = set()
    for text, facts in cases:
        for budget in (400, 900, 1500, 2500, 4096, 8192):
            expected = _reference_hit_flags(text, facts, budget)
            assert bench.score_output(text, facts, budget=budget).found == expected
            hits_seen.update(expected)
            compared += 1
    assert compared == len(cases) * 6
    assert hits_seen == {True, False}, "the comparison must include both hits and misses"
    leaves = bench.locate_leaves(edge)
    assert [(leaf.value, leaf.end_byte) for leaf in leaves if leaf.path in bench.SCORED_LEAVES] == _reference_scored_strings(edge)
