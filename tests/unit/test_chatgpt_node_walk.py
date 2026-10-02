"""The ChatGPT mapping walk is iterative and keeps the order the recursive walk had.

``_ordered_chatgpt_nodes`` used to call itself once per node, so a conversation
whose replies formed one long chain ran past the interpreter's recursion limit
at about a thousand messages and failed the whole import. The walk is a loop
over an explicit stack now. Nothing about the order may have changed, because
the order is the transcript, and the transcript is what is stored and hashed.

The reference below is the recursive function exactly as it was in v0.19.2.
Every test compares the loop with it on trees it can still walk, and the deep
tests show the reference failing where the loop does not, so they cannot pass
for a reason other than the fix.
"""

from __future__ import annotations

import random
import sys

import pytest

from alicebot_api.vnext_capture import _ordered_chatgpt_nodes


def _recursive_ordered_chatgpt_nodes(conversation: dict[str, object]) -> list[dict[str, object]]:
    """Return every mapping node in parent-before-child graph order.

    ChatGPT export mappings are keyed by opaque IDs whose lexical order has no
    conversational meaning. The current branch is visited first, while any
    alternate branches are retained afterward instead of silently discarded.
    """
    messages = conversation.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]

    raw_mapping = conversation.get("mapping")
    if not isinstance(raw_mapping, dict):
        return []
    mapping = {str(node_id): node for node_id, node in raw_mapping.items() if isinstance(node, dict)}
    if not mapping:
        return []

    children_by_parent: dict[str, list[str]] = {node_id: [] for node_id in mapping}
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if isinstance(parent, str) and parent in mapping:
            children_by_parent[parent].append(node_id)
    # Prefer the export's explicit child order where present, then append any
    # derived links that an incomplete export omitted from ``children``.
    for node_id, node in mapping.items():
        explicit = node.get("children")
        if not isinstance(explicit, list):
            continue
        ordered = [child for child in explicit if isinstance(child, str) and child in mapping]
        children_by_parent[node_id] = list(dict.fromkeys([*ordered, *children_by_parent[node_id]]))

    active_successor: dict[str, str] = {}
    current = conversation.get("current_node")
    if isinstance(current, str) and current in mapping:
        active_chain: list[str] = []
        seen: set[str] = set()
        while current in mapping and current not in seen:
            seen.add(current)
            active_chain.append(current)
            parent = mapping[current].get("parent")
            if not isinstance(parent, str):
                break
            current = parent
        active_chain.reverse()
        active_successor.update(zip(active_chain, active_chain[1:]))

    roots = [
        node_id
        for node_id, node in mapping.items()
        if not isinstance(node.get("parent"), str) or node.get("parent") not in mapping
    ]
    visited: set[str] = set()
    ordered_nodes: list[dict[str, object]] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        ordered_nodes.append(mapping[node_id])
        preferred = active_successor.get(node_id)
        children = children_by_parent[node_id]
        if preferred in children:
            children = [preferred, *(child for child in children if child != preferred)]
        for child in children:
            visit(child)

    for root in roots:
        visit(root)
    # Malformed/cyclic exports still retain every node once in source order.
    for node_id in mapping:
        visit(node_id)
    return ordered_nodes


def _ids(nodes: list[dict[str, object]]) -> list[object]:
    return [node["i"] for node in nodes]


def _chain(count: int) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for position in range(count):
        mapping[f"n{position}"] = {
            "i": position,
            "parent": f"n{position - 1}" if position else None,
            "children": [f"n{position + 1}"] if position + 1 < count else [],
        }
    return {"mapping": mapping, "current_node": f"n{count - 1}"}


def _random_mapping(rng: random.Random) -> dict[str, object]:
    """A mapping that is a tree, a forest, a graph with cycles, or damaged.

    Parents and child lists are drawn separately, so the same node can be
    listed under two parents, a child list can name an ancestor (a cycle), a
    parent can be missing from the mapping, and ``children`` can be absent,
    partial, duplicated or not a list. Nodes that are not objects are in the
    mapping too, and are dropped by the walk.
    """

    count = rng.randint(0, 40)
    ids = [f"id{position}" for position in range(count)]
    outside = ["ghost-a", "ghost-b"]
    mapping: dict[str, object] = {}
    for position, node_id in enumerate(ids):
        if rng.random() < 0.05:
            mapping[node_id] = rng.choice([None, "text", 7, ["list"]])
            continue
        node: dict[str, object] = {"i": position}
        parent_roll = rng.random()
        if parent_roll < 0.2:
            node["parent"] = None
        elif parent_roll < 0.3:
            pass
        elif parent_roll < 0.4:
            node["parent"] = rng.choice(outside)
        elif parent_roll < 0.45:
            node["parent"] = rng.choice([7, ["x"], {"y": 1}])
        elif parent_roll < 0.5:
            node["parent"] = node_id
        else:
            node["parent"] = rng.choice(ids)
        children_roll = rng.random()
        if children_roll < 0.15:
            pass
        elif children_roll < 0.2:
            node["children"] = rng.choice([None, "id1", 3, {"id1": 1}])
        else:
            pool = ids + outside + [5, None]
            node["children"] = [rng.choice(pool) for _ in range(rng.randint(0, 5))]
        mapping[node_id] = node
    conversation: dict[str, object] = {"mapping": mapping}
    current_roll = rng.random()
    if current_roll < 0.6 and ids:
        conversation["current_node"] = rng.choice(ids)
    elif current_roll < 0.7:
        conversation["current_node"] = "ghost-a"
    elif current_roll < 0.8:
        conversation["current_node"] = 12
    return conversation


def _consistent_tree(rng: random.Random) -> dict[str, object]:
    """A well-formed export: parents and child lists agree, inserted shuffled."""

    count = rng.randint(1, 60)
    parents: list[int | None] = [None]
    for position in range(1, count):
        parents.append(rng.randrange(position) if rng.random() < 0.9 else None)
    children: dict[int, list[int]] = {position: [] for position in range(count)}
    for position, parent in enumerate(parents):
        if parent is not None:
            children[parent].append(position)
    order = list(range(count))
    rng.shuffle(order)
    mapping: dict[str, object] = {}
    for position in order:
        listed = list(children[position])
        rng.shuffle(listed)
        if listed and rng.random() < 0.3:
            listed = listed[: rng.randint(0, len(listed))]
        node: dict[str, object] = {
            "i": position,
            "parent": None if parents[position] is None else f"id{parents[position]}",
        }
        if rng.random() < 0.9:
            node["children"] = [f"id{child}" for child in listed]
        mapping[f"id{position}"] = node
    return {"mapping": mapping, "current_node": f"id{rng.randrange(count)}"}


def test_the_loop_returns_the_recursive_order_on_random_graphs() -> None:
    """Several thousand mappings, damaged ones included, in the same order.

    Mutation that must fail it: iterate the children without reversing them
    (``stack.extend(children_in_visit_order(node_id))``), which walks each
    node's children last to first. Marking a node visited when it is pushed
    instead of when it is popped must also fail it, because the generator
    lists the same node under several parents.
    """

    rng = random.Random(20261001)
    shared_node_cases = 0
    cycle_cases = 0
    for case in range(4000):
        conversation = _random_mapping(rng) if case % 2 else _consistent_tree(rng)
        expected = _ids(_recursive_ordered_chatgpt_nodes(conversation))
        actual = _ids(_ordered_chatgpt_nodes(conversation))
        assert actual == expected, f"case {case}"
        mapping = conversation["mapping"]
        assert isinstance(mapping, dict)
        listed = [
            child
            for node in mapping.values()
            if isinstance(node, dict) and isinstance(node.get("children"), list)
            for child in node["children"]
            if isinstance(child, str) and child in mapping
        ]
        shared_node_cases += len(listed) != len(set(listed))
        cycle_cases += any(
            isinstance(node, dict) and node.get("parent") == node_id for node_id, node in mapping.items()
        )
    # The generator has to reach the shapes the claim is about, or a loop that
    # breaks on them would pass.
    assert shared_node_cases > 200
    assert cycle_cases > 50


def test_the_loop_returns_the_recursive_order_through_the_messages_shape() -> None:
    """A conversation with a ``messages`` list never touches the mapping.

    Mutation that must fail it: read ``mapping`` before ``messages``.
    """

    conversation = {"messages": [{"i": 1}, "x", {"i": 2}], "mapping": {"a": {"i": 9, "parent": None}}}

    assert _ids(_ordered_chatgpt_nodes(conversation)) == _ids(_recursive_ordered_chatgpt_nodes(conversation))
    assert _ids(_ordered_chatgpt_nodes(conversation)) == [1, 2]


def test_a_chain_of_three_thousand_messages_is_walked_in_order() -> None:
    """The reference runs out of stack on this chain. The loop does not.

    Mutation that must fail it: put the recursive walk back. The loop raises
    ``RecursionError`` at the depth the reference does.
    """

    conversation = _chain(3000)

    with pytest.raises(RecursionError):
        _recursive_ordered_chatgpt_nodes(conversation)

    assert _ids(_ordered_chatgpt_nodes(conversation)) == list(range(3000))


def test_a_chain_of_a_hundred_thousand_messages_is_walked_in_order() -> None:
    """No depth in the data reaches the interpreter's recursion limit."""

    conversation = _chain(100_000)

    assert _ids(_ordered_chatgpt_nodes(conversation)) == list(range(100_000))


def test_the_loop_matches_the_recursive_order_on_a_deep_branching_tree() -> None:
    """A spine of 3,000 nodes with a leaf hanging off each, and a cycle at the end.

    The reference is run with a raised recursion limit, which it needs at this
    depth, so the two orders are compared where the reference can still answer.

    Mutation that must fail it: iterate the children without reversing them.
    """

    count = 3000
    mapping: dict[str, object] = {}
    for position in range(count):
        spine = f"s{position}"
        leaf = f"l{position}"
        spine_children = [leaf] + ([f"s{position + 1}"] if position + 1 < count else [f"s{count // 2}"])
        mapping[spine] = {
            "i": position,
            "parent": f"s{position - 1}" if position else None,
            "children": spine_children,
        }
        mapping[leaf] = {"i": count + position, "parent": spine, "children": []}
    conversation = {"mapping": mapping, "current_node": f"s{count - 1}"}

    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(50_000)
    try:
        expected = _ids(_recursive_ordered_chatgpt_nodes(conversation))
    finally:
        sys.setrecursionlimit(previous_limit)

    assert _ids(_ordered_chatgpt_nodes(conversation)) == expected
    assert len(expected) == 2 * count
