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


def _fingerprint(order: str, **changes: object) -> dict[str, object]:
    """A complete fingerprint for one import order. Every field but the order is shared."""

    fp: dict[str, object] = {key: f"same-{key}" for key in bench.SAME_ACROSS_ORDERS}
    fp["import_order"] = order
    fp.update(changes)
    return fp


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
            json.dumps({"schema": bench.OUTPUTS_SCHEMA, "fingerprint": _fingerprint(label), "outputs": body}),
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


# The hit rule, the columns and the byte boundary --------------------------
#
# G1 and G2 count hits, and the PR table quotes facts found and bytes per call. These tests
# pin each of those by a case where the wrong rule gives another number, because the committed
# fixture questions are all-or-nothing and would not tell ``all`` from ``any``.


def _two_fact_question_set(tmp_path: Path) -> bench.QuestionSet:
    def question(qid: str, facts: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "id": qid,
            "question": f"question {qid}?",
            "keyword_query": f"kw {qid}",
            "facts": [{"fact": f"f{index}", "anchors": [{"text": text, "file": "x.md"}]} for index, (text, _) in enumerate(facts)],
        }

    path = _write_questions(
        tmp_path / "facts.json",
        [
            question("both_facts", [(ANCHOR, "x.md"), ("the ladder is in the shed", "x.md")]),
            question("one_of_two", [(ANCHOR, "x.md"), ("a sentence the output never holds", "x.md")]),
            question("single", [("the ladder is in the shed", "x.md")]),
        ],
    )
    return bench.load_questions(path)


def test_a_question_is_a_hit_only_when_every_fact_is_present(tmp_path: Path) -> None:
    """G1 and G2 count hits, and a hit needs every required fact (``hit_rule`` is ``all_facts``).

    Mutation: change ``OutputScore.hit`` from ``all(self.found)`` to ``any(self.found)``. The
    question with one fact of two would then count as a hit, and the committed fixture questions
    would not notice, because each of them is all or nothing.
    """

    text = _output(sources=[_source(excerpt=f"the {ANCHOR} sits on the sill")])
    two = bench.score_output(text, [_fact(ANCHOR), _fact("a sentence the output never holds")], budget=8192)
    assert two.found == (True, False)
    assert two.hit is False
    both = bench.score_output(text, [_fact(ANCHOR), _fact("on the sill")], budget=8192)
    assert both.found == (True, True) and both.hit is True
    neither = bench.score_output(text, [_fact("not here"), _fact("nor here")], budget=8192)
    assert neither.found == (False, False) and neither.hit is False
    assert _gates().data["tier1"]["hit_rule"] == "all_facts"

    qset = _two_fact_question_set(tmp_path)
    holds_first = _output(sources=[_source(excerpt=f"the {ANCHOR} sits on the sill")])
    holds_second = _output(sources=[_source(excerpt="the ladder is in the shed")])
    holds_both = _output(sources=[_source(excerpt=f"the {ANCHOR} sits here and the ladder is in the shed")])
    outputs = {
        "both_facts": {"verbatim": holds_both},
        "one_of_two": {"verbatim": holds_first},
        "single": {"verbatim": holds_second},
    }
    cell = bench.score_set(outputs, qset, variant="verbatim", budget=8192)
    assert cell["per_question"] == {"both_facts": True, "one_of_two": False, "single": True}
    assert cell["hits"] == 2
    assert (cell["facts_found"], cell["facts_total"]) == (4, 5)


def test_a_scored_string_counts_when_it_ends_exactly_at_the_budget_and_not_one_byte_later() -> None:
    """The budget is a byte count over the whole serialized output, and the boundary byte is inside.

    The server's JSON is ASCII (non-ASCII letters arrive as escapes), so the end of a string in bytes
    is its end in characters, found here with ``str.index`` and no help from the scorer.

    Mutation: ``leaf.end_byte < budget`` (the boundary byte falls outside), or ``<= budget + 1`` (one
    byte too generous), in ``score_output``.
    """

    body = f"the {ANCHOR} is in the shed and the caf\u00e9 is shut"
    text = _output(sources=[_source(excerpt=body, title="the caf\u00e9 note")])
    assert text.isascii()
    leaf = next(item for item in bench.locate_leaves(text) if item.path == "sources[].excerpt")
    literal = json.dumps(leaf.value)
    expected_end = text.index(literal) + len(literal)
    assert leaf.end_byte == expected_end == len(text[:expected_end].encode("utf-8"))
    facts = [_fact(ANCHOR)]
    assert bench.score_output(text, facts, budget=expected_end).found == (True,)
    assert bench.score_output(text, facts, budget=expected_end - 1).found == (False,)
    assert bench.score_output(text, facts, budget=expected_end + 1).found == (True,)
    assert bench.score_output(text, facts, budget=expected_end - 1).bytes_total == len(text.encode("utf-8"))


def test_empty_anchors_never_score_even_when_a_loader_was_not_asked() -> None:
    """An anchor that is empty after whitespace is collapsed matches every string, so it is skipped.

    Mutation: drop the ``if (needle := ...)`` guard in ``score_output``. An empty anchor would then
    be found in any scored string.
    """

    text = _output(sources=[_source(excerpt="something is here")])
    assert bench.score_output(text, [_fact("")], budget=8192).found == (False,)
    assert bench.score_output(text, [_fact("   ", "\n")], budget=8192).found == (False,)
    assert bench.score_output(text, [_fact("", "something is here")], budget=8192).found == (True,)


def _reference_cell(outputs: dict[str, dict[str, str]], qset: bench.QuestionSet, variant: str, budget: int) -> dict[str, Any]:
    """Tier 1 for one cell by the independent reader of TH12 and plain counting."""

    questions = qset.questions
    hits = facts_found = facts_total = floor_hits = 0
    bytes_sum = 0
    documents = passages = 0
    for index, question in enumerate(questions):
        text = outputs[question.id][variant]
        flags = _reference_hit_flags(text, list(question.facts), budget)
        hits += 1 if all(flags) else 0
        facts_found += sum(flags)
        facts_total += len(question.facts)
        bytes_sum += len(text.encode("utf-8"))
        sources = json.loads(text).get("sources", [])
        documents += len({entry["id"] for entry in sources})
        passages += len(sources)
        neighbour = outputs[questions[(index + 1) % len(questions)].id][variant]
        floor_hits += 1 if all(_reference_hit_flags(neighbour, list(question.facts), budget)) else 0
    count = len(questions)
    return {
        "hits": hits,
        "facts_found": facts_found,
        "facts_total": facts_total,
        "floor_hits": floor_hits,
        "bytes_per_call": bytes_sum / count,
        "documents_per_call": documents / count,
        "passages_per_call": passages / count,
    }


def test_the_reported_columns_equal_an_independent_count_on_the_committed_outputs() -> None:
    """Hits, facts found, floor, bytes, documents and passages per call, at every budget, by two counts.

    The reference counts with the second reader of TH12 and with ``len`` and ``set``, and the budgets
    run from tight (where only some facts fit) to the gate budget, so hits and facts found differ.

    Mutation: ``bytes_sum += budget`` or ``facts_found += 0`` in ``score_set``, ``hit`` as
    ``any``, a floor taken against the question's own output, or documents counted as passages.
    """

    qset = bench.load_questions(QUESTIONS)
    outputs = json.loads(OUTPUTS.read_text())["outputs"]
    seen_partial = False
    for variant in bench.VARIANTS:
        for budget in (300, 700, 1200, 1800, 2500, 4096, 8192):
            expected = _reference_cell(outputs, qset, variant, budget)
            cell = bench.score_set(outputs, qset, variant=variant, budget=budget)
            for key, value in expected.items():
                assert cell[key] == pytest.approx(value), (variant, budget, key)
            assert cell["n"] == 12
            assert list(cell["per_question"]) == [question.id for question in qset.questions]
            assert sum(cell["per_question"].values()) == cell["hits"]
            seen_partial = seen_partial or (0 < cell["hits"] < 12 and cell["facts_found"] > cell["hits"])
    assert seen_partial, "some budget must leave questions partly answered, or hit and fact counts cannot differ"
    # The numbers of the committed outputs at the two gate budgets, written out.
    for variant, bytes_per_call, documents_per_call in (("verbatim", 2274.0, 4.0), ("keyword", 2097.0, 47 / 12)):
        for budget in (4096, 8192):
            cell = bench.score_set(outputs, qset, variant=variant, budget=budget)
            assert (cell["hits"], cell["facts_found"], cell["facts_total"], cell["floor_hits"]) == (12, 18, 18, 0)
            assert cell["bytes_per_call"] == pytest.approx(bytes_per_call, abs=1.0)
            assert cell["documents_per_call"] == pytest.approx(documents_per_call)
    report = bench.score_outputs_document(json.loads(OUTPUTS.read_text()), qset, _gates())
    printed = bench.format_report("order sorted", report, _gates(), per_question=False)
    assert "12/12" in printed and "18/18" in printed and "0/12" in printed


def test_the_flag_thresholds_sit_exactly_at_12_characters_and_3_files() -> None:
    """An anchor is flagged below 12 characters and in more than 3 files, and not at the limit.

    Mutation: ``len(text) <= min`` or ``len(text) < min - 1`` (a limit off by one), or
    ``files_holding >= max`` or ``> max + 1``, in ``verify_anchors``.
    """

    gates = _gates()
    assert (gates.anchor_min_length, gates.anchor_max_files) == (12, 3)
    twelve, eleven = "twelve chars", "eleven char"
    assert (len(twelve), len(eleven)) == (12, 11)
    snapshot = {
        "a.md": f"{twelve} and {eleven} in one file. Shared across three files. Shared across four files.",
        "b.md": "Shared across three files. Shared across four files.",
        "c.md": "Shared across three files. Shared across four files.",
        "d.md": "Shared across four files.",
    }
    questions = bench.QuestionSet(
        set_id="t",
        sha256="0" * 64,
        questions=tuple(
            bench.Question(
                id=qid,
                question="unrelated?",
                keyword_query=None,
                kind=None,
                facts=(bench.Fact(label="f", anchors=(bench.Anchor(text=text, file="a.md"),)),),
            )
            for qid, text in (
                ("twelve", twelve),
                ("eleven", eleven),
                ("three_files", "Shared across three files"),
                ("four_files", "Shared across four files"),
            )
        ),
    )
    report = bench.verify_anchors(questions, snapshot, gates)
    flags = {row["question"]: row["flags"] for row in report["rows"]}
    files = {row["question"]: row["files"] for row in report["rows"]}
    assert flags == {"twelve": [], "eleven": ["short"], "three_files": [], "four_files": ["many_files"]}
    assert files["three_files"] == 3 and files["four_files"] == 4


def test_the_loader_refuses_duplicate_ids_empty_anchors_and_questions_with_no_facts(tmp_path: Path) -> None:
    """A question set that could score by accident is refused when it is read.

    Mutation: remove the duplicate-id check, the ``not anchor.text.strip()`` check, the no-anchor
    check or the no-facts check from ``load_questions``. An empty anchor would hit every time and a
    repeated id would overwrite another question's output.
    """

    def facts(*anchors: str) -> list[dict[str, Any]]:
        return [{"fact": "f", "anchors": [{"text": text, "file": "a.md"} for text in anchors]}]

    def entry(qid: str, fact_list: list[dict[str, Any]]) -> dict[str, Any]:
        return {"id": qid, "question": "q?", "keyword_query": "kw", "facts": fact_list}

    cases: dict[str, tuple[list[dict[str, Any]], str]] = {
        "duplicate": ([entry("q1", facts("x")), entry("q1", facts("y"))], "duplicate question id q1"),
        "empty anchor": ([entry("q1", facts(""))], "no usable anchor"),
        "blank anchor": ([entry("q1", facts("   \n"))], "no usable anchor"),
        "one blank of two": ([entry("q1", facts("real anchor", " "))], "no usable anchor"),
        "no anchors": ([entry("q1", [{"fact": "f", "anchors": []}])], "no usable anchor"),
        "no facts": ([entry("q1", [])], "has no facts"),
        "empty set": ([], "empty"),
    }
    for name, (questions, message) in cases.items():
        path = _write_questions(tmp_path / f"{name.replace(' ', '_')}.json", questions)
        with pytest.raises(bench.BenchError, match=message):
            bench.load_questions(path)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": "something-else", "questions": []}), encoding="utf-8")
    with pytest.raises(bench.BenchError, match="not an alice-bench-questions/1 file"):
        bench.load_questions(wrong)
    ok = bench.load_questions(_write_questions(tmp_path / "ok.json", [entry("q1", facts("x")), entry("q2", facts("y"))]))
    assert [question.id for question in ok.questions] == ["q1", "q2"]
    with pytest.raises(bench.BenchError, match="no keyword query"):
        bench.Question(id="q", question="q", keyword_query=None, kind=None, facts=()).query_for("keyword")


def test_outputs_for_another_question_set_or_with_a_question_missing_are_refused() -> None:
    """Scoring never reads outputs that were made for other questions.

    Mutation: remove the question-set hash comparison, the missing-question check or the schema
    check in ``score_outputs_document``. Outputs of an older question set would then score quietly.
    """

    qset = bench.load_questions(QUESTIONS)
    document = json.loads(OUTPUTS.read_text())
    assert "question_set_sha256" not in document, "the committed outputs predate any hash and are accepted"
    assert bench.score_outputs_document(document, qset, _gates())["variants"]
    stamped = {**document, "question_set_sha256": qset.sha256}
    assert bench.score_outputs_document(stamped, qset, _gates())["variants"]
    with pytest.raises(bench.BenchError, match="different question set"):
        bench.score_outputs_document({**document, "question_set_sha256": "0" * 64}, qset, _gates())
    missing = {**document, "outputs": {key: value for key, value in document["outputs"].items() if key != "f03"}}
    with pytest.raises(bench.BenchError, match="nothing for question f03"):
        bench.score_outputs_document(missing, qset, _gates())
    with pytest.raises(bench.BenchError, match="not an alice-bench-outputs/1 file"):
        bench.score_outputs_document({**document, "schema": "other/1"}, qset, _gates())
    only_verbatim = {
        **document,
        "outputs": {key: {"verbatim": value["verbatim"]} for key, value in document["outputs"].items()},
    }
    assert list(bench.score_outputs_document(only_verbatim, qset, _gates())["variants"]) == ["verbatim"]


# Which outputs may be compared, and what ``score --json`` shows ---------------


def _write_outputs(path: Path, order: str, **fp_changes: object) -> str:
    outputs = json.loads(OUTPUTS.read_text())["outputs"]
    path.write_text(
        json.dumps({"schema": bench.OUTPUTS_SCHEMA, "fingerprint": _fingerprint(order, **fp_changes), "outputs": outputs}),
        encoding="utf-8",
    )
    return str(path)


def test_outputs_are_compared_only_when_one_checkout_switch_and_corpus_made_them(tmp_path: Path) -> None:
    """The minimum over import orders is read across outputs that measured the same thing, in distinct orders.

    Mutation: drop one key from ``SAME_ACROSS_ORDERS`` (the commit, the dirty flag, the switch, the
    corpus or the question set), skip the distinct-order check, or let a missing fingerprint pass.
    Outputs from two commits would then give a minimum that names neither.
    """

    document = {"fingerprint": _fingerprint("sorted")}
    bench.require_comparable_outputs([("a.json", document), ("b.json", {"fingerprint": _fingerprint("reverse")})])
    bench.require_comparable_outputs([("only.json", {"outputs": {}})])
    for key in bench.SAME_ACROSS_ORDERS:
        other = {"fingerprint": _fingerprint("reverse", **{key: "something else"})}
        with pytest.raises(bench.BenchError, match=rf"did not measure the same thing \({key} differ\)"):
            bench.require_comparable_outputs([("a.json", document), ("b.json", other)])
    with pytest.raises(bench.BenchError, match="same import order"):
        bench.require_comparable_outputs([("a.json", document), ("b.json", {"fingerprint": _fingerprint("sorted")})])
    with pytest.raises(bench.BenchError, match="b.json has no fingerprint"):
        bench.require_comparable_outputs([("a.json", document), ("b.json", {"outputs": {}})])
    partial = {"fingerprint": {"import_order": "reverse"}}
    with pytest.raises(bench.BenchError, match="lacks git_sha"):
        bench.require_comparable_outputs([("a.json", document), ("b.json", partial)])

    first = _write_outputs(tmp_path / "first.json", "sorted")
    second = _write_outputs(tmp_path / "second.json", "reverse", git_sha="another commit")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert bench.main(["score", "--questions", str(QUESTIONS), "--outputs", first, second]) == bench.EXIT_REFUSED
    assert stdout.getvalue() == "", "a refused comparison prints no table"
    assert bench.main(["score", "--questions", str(QUESTIONS), "--outputs", first, first]) == bench.EXIT_REFUSED


def test_score_json_leaves_out_per_question_results_unless_they_are_asked_for(tmp_path: Path) -> None:
    """``--per-question`` is the dev only opt in, so the JSON never carries a hit or miss per question without it.

    Mutation: ``score --json`` includes ``per_question`` whatever the flag says (drop
    ``without_per_question`` in ``_cmd_score``), or drops it even when asked.
    """

    first = _write_outputs(tmp_path / "first.json", "sorted")
    second = _write_outputs(tmp_path / "second.json", "reverse")

    def run(*extra: str) -> dict[str, Any]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert bench.main(["score", "--questions", str(QUESTIONS), "--outputs", first, second, "--json", *extra]) == 0
        return dict(json.loads(stdout.getvalue()))

    plain = run()
    asked = run("--per-question")
    assert plain["minimum_hits"] == asked["minimum_hits"] == {"verbatim": {"4096": 12, "8192": 12}, "keyword": {"4096": 12, "8192": 12}}
    for order in ("sorted", "reverse"):
        for variant in bench.VARIANTS:
            for cell in plain["orders"][order]["variants"][variant].values():
                assert "per_question" not in cell and cell["hits"] == 12
            for cell in asked["orders"][order]["variants"][variant].values():
                assert set(cell["per_question"]) == {f"f{number:02d}" for number in range(1, 13)}
    assert "per_question" not in json.dumps(plain)


# Documents and passages, the printed table, the per question view -------------


def test_documents_and_passages_per_call_are_averaged_separately_over_the_set(tmp_path: Path) -> None:
    """The set level columns count distinct documents and passage entries apart.

    One question's output holds three passages of two documents, so the two columns differ. The
    committed outputs hold one passage for each document and cannot tell them apart.

    Mutation: add ``result.passages`` to the documents total of ``score_set`` or ``result.documents``
    to the passages total.
    """

    qset = _two_fact_question_set(tmp_path)
    repeated = _output(
        sources=[
            _source(excerpt="first part", source_id="same"),
            _source(excerpt="second part", source_id="same"),
            _source(excerpt="other", source_id="other"),
        ]
    )
    single = _output(sources=[_source(excerpt="alone", source_id="only")])
    empty = _output()
    outputs = {
        "both_facts": {"verbatim": repeated},
        "one_of_two": {"verbatim": single},
        "single": {"verbatim": empty},
    }
    cell = bench.score_set(outputs, qset, variant="verbatim", budget=8192)
    assert cell["documents_per_call"] == pytest.approx((2 + 1 + 0) / 3)
    assert cell["passages_per_call"] == pytest.approx((3 + 1 + 0) / 3)
    assert cell["bytes_per_call"] == pytest.approx(sum(len(text.encode()) for text in (repeated, single, empty)) / 3)


def test_the_printed_table_has_its_columns_in_order_and_names_the_dev_only_view(tmp_path: Path) -> None:
    """One row of the table, written out, and the per question lines at the gate budget only.

    The first question's excerpt sits between 4 KB and 8 KB (a long entities list comes first), so it
    is a miss at 4096 and a hit at 8192, and the per question view has to read the gate budget.

    Mutation: swap two columns of ``format_report``, print the per question lines for the first budget
    instead of ``gate_budget_bytes``, or print them without ``--per-question``.
    """

    entities = [
        {"entity_type": "other", "id": f"e{number}", "mention_count": 1, "name": f"Invented Person {number:03d}"}
        for number in range(70)
    ]
    late = _output(sources=[_source(excerpt=f"the {ANCHOR} is in the shed")], entities=entities)
    assert 4096 < late.index("violet lantern") < 8192
    never = _output(sources=[_source(excerpt="nothing relevant")])
    qset = _two_question_set(tmp_path)
    outputs_path = tmp_path / "outputs.json"
    outputs_path.write_text(
        json.dumps(
            {
                "schema": bench.OUTPUTS_SCHEMA,
                "outputs": {"q1": {"verbatim": late}, "q2": {"verbatim": never}},
            }
        ),
        encoding="utf-8",
    )
    report = bench.score_outputs_document(json.loads(outputs_path.read_text()), qset, _gates())
    plain = bench.format_report("order sorted", report, _gates(), per_question=False)
    row_4096 = next(line for line in plain.splitlines() if line.startswith("verbatim") and " 4096 " in line)
    row_8192 = next(line for line in plain.splitlines() if line.startswith("verbatim") and " 8192 " in line)
    mean_bytes = f"{(len(late.encode()) + len(never.encode())) / 2:.0f}"
    assert row_4096.split() == ["verbatim", "4096", "0/2", "0/2", "0/2", mean_bytes, "1.00"]
    assert row_8192.split() == ["verbatim", "8192", "1/2", "1/2", "0/2", mean_bytes, "1.00"]
    assert "per question" not in plain and "  q1 hit" not in plain
    shown = bench.format_report("order sorted", report, _gates(), per_question=True)
    assert "per question, verbatim, 8192 bytes (dev only)" in shown
    assert "  q1 hit" in shown and "  q2 miss" in shown
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert bench.main(["score", "--questions", str(tmp_path / "two.json"), "--outputs", str(outputs_path)]) == 0
    assert "per question" not in stdout.getvalue()
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = bench.main(["score", "--questions", str(tmp_path / "two.json"), "--outputs", str(outputs_path), "--per-question"])
    assert code == 0 and "  q1 hit" in stdout.getvalue()


def test_a_build_with_strict_flags_fails_on_an_unapproved_flag_and_a_single_question_has_no_floor(tmp_path: Path) -> None:
    """TH5 and TH6. ``build --strict-flags`` refuses like ``anchors --strict-flags``, and one question has no rotation.

    Mutation: ignore ``args.strict_flags`` in ``_cmd_build``, print a floor for a single question, or
    take the minimum over orders that lack one of the variants as if they had it.
    """

    flagged = _write_questions(tmp_path / "f.json", [_q("q", "what?", [("tide-keeping.md", "ledger")])])
    for number, (extra, expected) in enumerate(((["--strict-flags"], bench.EXIT_REFUSED), ([], 0))):
        run = tmp_path / f"run-{number}"
        args = ["build", "--run-dir", str(run), "--corpus", str(CORPUS), "--questions", str(flagged), *extra]
        assert bench.main(args) == expected

    single = bench.load_questions(_write_questions(tmp_path / "one.json", [_q("q1", "x?", [("x.md", ANCHOR)])]))
    document = {
        "schema": bench.OUTPUTS_SCHEMA,
        "outputs": {"q1": {"verbatim": _output(sources=[_source(excerpt=f"the {ANCHOR} is here")])}},
    }
    printed = bench.format_report("order sorted", bench.score_outputs_document(document, single, _gates()), _gates(), per_question=False)
    assert all(line.split()[4] == "n/a" for line in printed.splitlines() if line.startswith("verbatim"))

    both = {"variants": {"verbatim": {"4096": {"hits": 3}, "8192": {"hits": 5}}, "keyword": {"4096": {"hits": 4}, "8192": {"hits": 6}}}}
    only_verbatim = {"variants": {"verbatim": {"4096": {"hits": 1}, "8192": {"hits": 2}}}}
    minimums = bench.minimum_across_orders([("a", both), ("b", only_verbatim)], _gates())
    assert minimums == {"verbatim": {"4096": 1, "8192": 2}, "keyword": {"4096": 4, "8192": 6}}
