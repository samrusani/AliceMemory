"""The continuity page asks the what-changed route the way the route is built and reads only what it returns.

The web console's own tests run in its CI job and mock the route. This file reads the console's source and the route
as it is shipped (the application's OpenAPI document) and fails when the two stop agreeing: the request path and
method, the query and body fields, every field of the answer the page types, and the topic box the page offers.

Mutations: rename one field of ``WhatChangedStep`` in ``apps/web/lib/api.ts``; change the path or the body of
``postWhatChanged``; rename the ``what_changed`` field of the topic box in the page; print the id of an upcoming or
disputed step that the history does not carry.
"""

from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.main import app

ROOT = Path(__file__).resolve().parents[2]
API_TS = (ROOT / "apps/web/lib/api.ts").read_text(encoding="utf-8")
PAGE_TSX = (ROOT / "apps/web/app/continuity/page.tsx").read_text(encoding="utf-8")
ROUTE = "/v0/vnext/changes"


def _balanced(source: str, start: int) -> str:
    """The text between the brace at ``start`` and its closing brace."""

    assert source[start] == "{"
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : index]
    raise AssertionError("unbalanced braces")


def _fields(body: str) -> dict[str, tuple[bool, str]]:
    """The fields at the top level of an object type body: name -> (optional, type text)."""

    found: dict[str, tuple[bool, str]] = {}
    depth = 0
    start = 0
    pieces: list[str] = []
    for index, char in enumerate(body):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == ";" and depth == 0:
            pieces.append(body[start:index])
            start = index + 1
    if body[start:].strip():
        pieces.append(body[start:])
    for piece in pieces:
        match = re.match(r"\s*(\w+)(\?)?:\s*(.*)", piece, re.S)
        assert match, piece
        found[match.group(1)] = (match.group(2) == "?", match.group(3).strip())
    return found


def _type_body(name: str) -> str:
    match = re.search(rf"export type {name} = \{{", API_TS)
    assert match, name
    return _balanced(API_TS, match.end() - 1)


def _schemas() -> tuple[dict[str, object], dict[str, object]]:
    spec = app.openapi()
    post = spec["paths"][ROUTE]["post"]
    ref = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    answer = spec["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return post, answer


def test_the_route_takes_a_topic_in_a_post_body_and_the_user_id_in_the_query() -> None:
    post, _answer = _schemas()
    assert set(app.openapi()["paths"][ROUTE]) == {"post"}
    parameters = {item["name"]: item for item in post["parameters"]}
    assert parameters["user_id"]["in"] == "query" and parameters["user_id"]["required"] is True
    assert parameters["authorization"]["in"] == "header" and parameters["authorization"]["required"] is False
    body_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body = app.openapi()["components"]["schemas"][body_ref.rsplit("/", 1)[1]]
    assert set(body["properties"]) == {"topic"}


def test_the_web_client_posts_what_the_route_takes() -> None:
    start = API_TS.index("export function postWhatChanged")
    function = API_TS[start : API_TS.index("\n}\n", start)]
    assert f'"{ROUTE}"' in function
    assert 'method: "POST"' in function
    assert "JSON.stringify({ topic })" in function
    assert "{ user_id: userId }" in function


def test_the_web_types_name_every_field_the_route_returns_that_the_page_reads() -> None:
    _post, answer = _schemas()
    properties = answer["properties"]
    required = set(answer["required"])
    top = _fields(_type_body("WhatChangedAnswer"))
    assert set(top) == set(properties)
    assert {name for name, (optional, _text) in top.items() if not optional} == required

    chain_properties = properties["chains"]["items"]["properties"]
    chain_type = top["chains"][1]
    chain = _fields(_balanced(chain_type, chain_type.index("{")))
    assert set(chain) == set(chain_properties)

    step_properties = chain_properties["history"]["items"]["properties"]
    step = _fields(_type_body("WhatChangedStep"))
    # The page does not read the writer of a step, so the type leaves it out.
    assert set(step) == set(step_properties) - {"writer"}

    reason_type = step["reason"][1]
    reason = _fields(_balanced(reason_type, reason_type.index("{")))
    reason_properties = next(
        option["properties"] for option in step_properties["reason"]["anyOf"] if option.get("type") == "object"
    )
    assert set(reason) == set(reason_properties)

    source = _fields(_type_body("WhatChangedSource"))
    assert set(source) == set(step_properties["sources"]["items"]["properties"])


def test_the_page_reads_the_topic_from_the_address_and_offers_a_box_that_sets_it() -> None:
    assert "normalizeParam(params.what_changed)" in PAGE_TSX
    assert "postWhatChanged(apiConfig.apiBaseUrl, apiConfig.userId, changedTopic)" in PAGE_TSX
    form = re.search(r'<form method="get" action="/continuity"[^>]*>(.*?)</form>', PAGE_TSX, re.S)
    assert form, "the topic box is a GET form that returns to the continuity page"
    assert 'name="what_changed"' in form.group(1)
    label = re.search(r'<label htmlFor="([^"]+)">Topic</label>', form.group(1))
    assert label and f'id="{label.group(1)}"' in form.group(1)


def test_the_page_prints_only_steps_the_history_carries() -> None:
    start = PAGE_TSX.index("function WhatChangedIdList")
    function = PAGE_TSX[start : PAGE_TSX.index("\nfunction WhatChangedStepView", start)]
    assert "chain.history.find(" in function
    assert ".filter(" in function
    # An id is a key and a lookup. It is never printed as text.
    assert not re.search(r":\s*id\s*\}", function)
    assert not re.search(r"\{\s*id\s*\}", function)
    assert not re.search(r"\{\s*ids\b", function.split("return (")[-1])
