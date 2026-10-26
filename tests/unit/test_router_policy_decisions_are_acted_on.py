"""A route that asks the agent policy for a decision acts on a blocked one.

``_vnext_policy_checked`` records the policy events and returns the decision. It never raises, so a route that does not
test the decision writes whatever it was asked to write while its own answer says ``blocked``: ``POST
/v0/vnext/agents/ingest-output`` did, for a ``read_only_agent`` key, until it tested the decision the way the other write
routes do. This scan reads every function under ``routers/`` and fails for a call of a helper that returns a decision
(``_vnext_policy_checked``, ``_vnext_exact_resource_policy``, ``_vnext_operator_gate`` or ``evaluate_agent_policy``) when
the function does not test the result afterwards and leave on a blocked one: ``return _vnext_permission_response(...)`` or a
``raise``, from inside the connection so the policy events of the refusal commit. A call whose result is not bound to a name
is not accepted either, apart from the one listed below.

What the scan does not judge: that the test comes before the first write (the PostgreSQL tests of each route count the rows
of a refused request), and a route that never asks for a decision. ``_vnext_authorized_artifact`` raises on a blocked
decision itself and is read here by its own body.

Mutations, each one alone: the ``blocked`` test taken out of ``ingest_vnext_agent_output`` (``test_the_scan_catches_the_ingest_route_defect``
and ``test_every_decision_a_router_asks_for_is_acted_on`` fail); the ``return`` of a guard turned into ``pass``; a new route
that calls ``_vnext_policy_checked`` and writes at once.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alicebot_api.vnext_agent_control import WRITE_ACTIONS

ROUTERS = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api" / "routers"
DECISION_HELPERS = frozenset(
    {"_vnext_policy_checked", "_vnext_exact_resource_policy", "_vnext_operator_gate", "evaluate_agent_policy"}
)

#: Calls whose result is handed to a writer instead of being tested in the route, by route function and helper. The
#: proposal route gives ``propose_memory`` a closure that returns the decision. ``propose_memory`` writes no row for a
#: blocked decision and the route answers ``_vnext_permission_response(outcome.decision)``.
HANDED_ON: dict[tuple[str, str], str] = {
    ("create_vnext_memory_proposal", "_vnext_policy_checked"): "propose_memory refuses to write for a blocked decision",
}

INGEST_GUARD = '''            if decision.decision == "blocked":
                return _vnext_permission_response(decision)
            ingest_payload = request.model_dump(mode="json")
'''


def _callee(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _walk_without_nested_functions(nodes: list[ast.stmt]):
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(child)


def _leaves_with_a_refusal(body: list[ast.stmt]) -> bool:
    """The branch ends the request: it returns ``_vnext_permission_response(...)`` or raises."""

    for node in _walk_without_nested_functions(body):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) and _callee(node.value) == "_vnext_permission_response":
            return True
    return False


def _is_the_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _tests_the_decision(test: ast.expr, name: str, helper: str) -> str | None:
    """``"refuses"`` when the test is true for a blocked decision, ``"allows"`` when it is true for one that is not."""

    for node in ast.walk(test):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op, right = node.ops[0], node.comparators[0]
        if helper == "_vnext_operator_gate":
            # The gate returns the refusal, or None when the caller may go on.
            if _is_the_name(node.left, name) and isinstance(op, ast.IsNot) and isinstance(right, ast.Constant) and right.value is None:
                return "refuses"
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "decision" and _is_the_name(left.value, name)):
            continue
        if isinstance(right, ast.Constant) and right.value == "blocked":
            if isinstance(op, ast.Eq):
                return "refuses"
            if isinstance(op, ast.NotEq):
                return "allows"
    return None


def _acts_on(top: ast.FunctionDef | ast.AsyncFunctionDef, name: str, helper: str, after_line: int) -> bool:
    for node in ast.walk(top):
        if not isinstance(node, ast.If) or node.lineno <= after_line:
            continue
        kind = _tests_the_decision(node.test, name, helper)
        if kind == "refuses" and _leaves_with_a_refusal(node.body):
            return True
        # The refused branch is the else: the allowed work is the body and the refusal follows or is the else.
        if kind == "allows" and helper != "_vnext_policy_checked" and any(
            not isinstance(statement, ast.Pass) for statement in node.body
        ):
            return True
    return False


def unacted_decisions(source: str) -> list[str]:
    """Every call of a decision helper in ``source`` whose route does not act on a blocked decision."""

    tree = ast.parse(source)
    problems: list[str] = []
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) or top.name in DECISION_HELPERS:
            continue
        parents = {child: node for node in ast.walk(top) for child in ast.iter_child_nodes(node)}
        asks_for_a_refusal = any(
            isinstance(node, ast.Call) and _callee(node) == "_vnext_permission_response" for node in ast.walk(top)
        )
        for call in ast.walk(top):
            if not isinstance(call, ast.Call) or _callee(call) not in DECISION_HELPERS:
                continue
            helper = str(_callee(call))
            site = f"{top.name}:{call.lineno} {helper}"
            parent = parents.get(call)
            bound = (
                parent.targets[0].id
                if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name)
                else None
            )
            if bound is None:
                if (top.name, helper) not in HANDED_ON or not asks_for_a_refusal:
                    problems.append(f"{site}: the result is not kept and tested")
            elif not _acts_on(top, bound, helper, call.lineno):
                problems.append(f"{site}: no later test of '{bound}' refuses a blocked decision")
    return problems


def _router_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(ROUTERS.glob("*.py"))}


def _helper_calls(source: str) -> list[tuple[str, str, str | None]]:
    """Function, helper and the ``action`` of each call, as written."""

    found: list[tuple[str, str, str | None]] = []
    for top in ast.parse(source).body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) or top.name in DECISION_HELPERS:
            continue
        for call in ast.walk(top):
            if isinstance(call, ast.Call) and _callee(call) in DECISION_HELPERS:
                action = next((ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg == "action"), None)
                found.append((top.name, str(_callee(call)), action))
    return found


def test_every_decision_a_router_asks_for_is_acted_on() -> None:
    problems = [f"{name}: {problem}" for name, source in _router_sources().items() for problem in unacted_decisions(source)]
    assert problems == []


def test_the_scan_reads_the_write_routes_it_is_meant_to_close() -> None:
    sources = _router_sources()
    calls = {name: _helper_calls(source) for name, source in sources.items()}
    policy_checked = [call for found in calls.values() for call in found if call[1] == "_vnext_policy_checked"]
    assert len(policy_checked) >= 17, "the scan found too few call sites to be reading the routers"
    writes = [call for call in policy_checked if call[2] is not None and call[2].strip("'\"") in WRITE_ACTIONS]
    assert len(writes) >= 12
    capture_routes = {function for function, _helper, action in calls["vnext_memories.py"] if action == "'source.capture'"}
    assert capture_routes == {"create_vnext_source", "ingest_vnext_agent_output"}
    assert {function for function, helper, _action in calls["vnext_memories.py"] if helper == "_vnext_operator_gate"} == {
        "get_vnext_source"
    }


def test_the_scan_catches_the_ingest_route_defect() -> None:
    """The route as it was: the decision is built, recorded and handed to the service with no test between."""

    source = _router_sources()["vnext_memories.py"]
    assert source.count(INGEST_GUARD) == 1
    assert unacted_decisions(source) == []
    problems = unacted_decisions(source.replace(INGEST_GUARD, '            ingest_payload = request.model_dump(mode="json")\n'))
    assert len(problems) == 1 and problems[0].startswith("ingest_vnext_agent_output:")


_ACTED_ON = '''
def route(request):
    with connection() as conn:
        decision = _vnext_policy_checked(store=conn, identity=None, action="source.capture")
        if decision.decision == "blocked":
            return _vnext_permission_response(decision)
        write(conn)
'''


def test_the_scan_accepts_a_route_that_tests_and_returns_the_refusal() -> None:
    assert unacted_decisions(_ACTED_ON) == []
    assert unacted_decisions(_ACTED_ON.replace("return _vnext_permission_response(decision)", "raise Refused(decision)")) == []


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(("        if decision.decision == \"blocked\":\n            return _vnext_permission_response(decision)\n", ""), id="no test"),
        pytest.param(("return _vnext_permission_response(decision)", "pass"), id="the branch does not leave"),
        pytest.param(("return _vnext_permission_response(decision)", "return None"), id="the branch returns something else"),
        pytest.param(('decision.decision == "blocked"', 'decision.decision == "allowed"'), id="another word"),
        pytest.param(('decision.decision == "blocked"', "decision.review_required"), id="another field"),
        pytest.param(("decision = _vnext_policy_checked(", "other = _vnext_policy_checked("), id="the test reads another name"),
        pytest.param(("decision = _vnext_policy_checked(", "_vnext_policy_checked("), id="the result is dropped"),
    ],
)
def test_the_scan_reports_a_route_that_does_not_act(changed: tuple[str, str]) -> None:
    old, new = changed
    assert old in _ACTED_ON
    assert len(unacted_decisions(_ACTED_ON.replace(old, new))) == 1


def test_the_scan_reports_a_test_that_comes_before_the_call() -> None:
    before = '''
def route(request):
    decision = None
    if decision is not None and decision.decision == "blocked":
        return _vnext_permission_response(decision)
    decision = _vnext_policy_checked(store=None, identity=None, action="source.capture")
    write()
'''
    assert len(unacted_decisions(before)) == 1


def test_the_scan_reads_the_operator_gate_and_the_filter_forms() -> None:
    gate = '''
def route():
    operator = _vnext_operator_gate(store, identity, route_path="/x")
    if operator is not None:
        return _vnext_permission_response(operator)
'''
    assert unacted_decisions(gate) == []
    assert len(unacted_decisions(gate.replace("return _vnext_permission_response(operator)", "pass"))) == 1
    filtered = '''
def route():
    verdict = _vnext_exact_resource_policy(identity=None, action="artifact.lookup", resource={})
    if verdict.decision != "blocked":
        keep.append(1)
'''
    assert unacted_decisions(filtered) == []
    assert len(unacted_decisions(filtered.replace("keep.append(1)", "pass"))) == 1


def test_a_result_handed_to_a_writer_is_accepted_only_where_it_is_listed() -> None:
    handed = '''
def create_vnext_memory_proposal(request):
    def authorize(candidate):
        return _vnext_policy_checked(store=None, identity=None, action="memory.propose")
    outcome = propose_memory(authorize=authorize)
    if outcome.memory is None:
        return _vnext_permission_response(outcome.decision)
'''
    assert unacted_decisions(handed) == []
    assert len(unacted_decisions(handed.replace("create_vnext_memory_proposal", "create_something_else"))) == 1
    assert len(unacted_decisions(handed.replace("_vnext_permission_response(outcome.decision)", "outcome"))) == 1
