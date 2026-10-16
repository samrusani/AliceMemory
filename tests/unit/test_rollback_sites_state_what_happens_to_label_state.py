"""Every product block that can roll back says what happens to the request's label state.

A read in a request caches the labels it judges. When a write inside a block is rolled back, a label cached
after that write describes a row that is gone, and the rest of the request would read a label that is too
permissive. ``store.savepoint()`` empties the cached labels when it rolls back. A block that opens its own
transaction or savepoint on the connection, or rolls the connection back, has to do the same, or say why it
does not need to.

The scan reads the syntax tree of the product code and judges each block on its own. A block is one of these:

* a call of ``.transaction(...)``, or a call of a name that was bound to the ``transaction`` attribute earlier in
  the same function or module (``begin = store.conn.transaction`` and then ``with begin():``, or
  ``getattr(conn, "transaction", None)``);
* a call of ``.rollback(...)``, or of a name bound to it the same way;
* a call of ``execute`` (any name that contains it, bound the same way too) whose first argument is, or is built
  from, a string that contains the word SAVEPOINT or ROLLBACK. The string can be written in the call, or held in a
  name that was assigned a string earlier in the same function or module (``sql = "ROLLBACK"`` and then
  ``conn.execute(sql)``).

Each such block has to be accounted for by something that belongs to it, not to the function around it:

* a ``# label state: <reason>`` comment on the line of the block, or in the comment lines directly above it
  (above the statement that holds the block when that statement spans several lines);
* a ``drop_label_state_on_rollback(...)`` item that comes before the block in the same ``with`` statement, or an
  enclosing ``with`` of the same function that has one;
* an enclosing ``try`` of the same function with a handler that calls ``label_savepoint_rolled_back``,
  ``invalidate_read_labels`` or ``drop_label_state_on_rollback``, and the statement that opens a savepoint just
  before such a ``try`` (what ``store.savepoint()`` does).

A comment or a helper call elsewhere in the function does not count for another block of that function.

What the scan cannot see: a transaction opened through a function of another module (the function's own block is
scanned, its call site is not), a name bound by tuple unpacking, a parameter or an attribute, a string built in
another module or by a call, a keyword split across two strings, a comment whose reason is wrong, and whether a handler or a wrapper really reaches the
store of the request. A reviewer still reads the reasons. The scan makes sure there is one for every block.

The behaviour of the blocks that do empty the state is tested in
``tests/integration/test_weekly_parent_index_invalidation_postgres.py`` and
``tests/unit/test_weekly_parent_index_invalidation.py``.

Mutations: leave ``drop_label_state_on_rollback`` out of ``capture_label_inputs`` or of ``_embedding_write_savepoint``;
delete one of the ``# label state:`` comments, including one in a function that holds two blocks.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps/api/src/alicebot_api"
MARKER = "# label state:"
WRAPPER = "drop_label_state_on_rollback"
EMPTIES_THE_STATE = frozenset({WRAPPER, "label_savepoint_rolled_back", "invalidate_read_labels"})
ROLLS_BACK_BY_SQL = re.compile(r"\b(SAVEPOINT|ROLLBACK)\b", re.IGNORECASE)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
TRY_STATEMENTS = tuple(item for item in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if item)


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    function: str
    shape: str
    accounted: bool


def _watched(attribute: str) -> bool:
    return attribute in {"transaction", "rollback"} or "execute" in attribute


def _scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """The function that holds ``node``, or the module."""

    current = parents.get(node)
    while current is not None and not isinstance(current, (*FUNCTIONS, ast.Module)):
        current = parents.get(current)
    assert current is not None
    return current


def _scopes_outward(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    scopes = [_scope_of(node, parents)]
    while not isinstance(scopes[-1], ast.Module):
        scopes.append(_scope_of(scopes[-1], parents))
    return scopes


def _getattr_name(node: ast.AST) -> str | None:
    """``getattr(x, "transaction", None)`` names ``transaction``."""

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def _watched_reference(value: ast.AST) -> set[str]:
    """The watched attributes a value is, when it is the attribute itself and not the result of calling it."""

    if isinstance(value, ast.Attribute):
        return {value.attr} if _watched(value.attr) else set()
    named = _getattr_name(value)
    if named is not None:
        return {named} if _watched(named) else set()
    if isinstance(value, ast.IfExp):
        return _watched_reference(value.body) | _watched_reference(value.orelse)
    if isinstance(value, ast.BoolOp):
        return set().union(*(_watched_reference(item) for item in value.values))
    return set()


class _Bindings:
    """What each plain ``name = value`` assignment of a function or module bound, for the aliases and strings."""

    def __init__(self, tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> None:
        self.parents = parents
        self.aliases: dict[tuple[ast.AST, str], set[str]] = {}
        self.values: dict[tuple[ast.AST, str], list[ast.AST]] = {}
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign):
                targets, value = [node.target], node.value
            if value is None:
                continue
            scope = _scope_of(node, parents)
            for target in targets:
                if isinstance(target, ast.Name):
                    key = (scope, target.id)
                    self.values.setdefault(key, []).append(value)
                    self.aliases.setdefault(key, set()).update(_watched_reference(value))

    def _binding_scope(self, name: str, node: ast.AST) -> ast.AST | None:
        for scope in _scopes_outward(node, self.parents):
            if (scope, name) in self.values:
                return scope
        return None

    def alias_names(self, name: str, node: ast.AST) -> set[str]:
        scope = self._binding_scope(name, node)
        return self.aliases.get((scope, name), set()) if scope is not None else set()

    def text(self, expression: ast.AST, node: ast.AST, seen: frozenset[tuple[ast.AST, str]] = frozenset()) -> str:
        """Every string written in ``expression``, and every string held by a name it uses."""

        parts: list[str] = []
        for child in ast.walk(expression):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                parts.append(child.value)
            elif isinstance(child, ast.Name):
                scope = self._binding_scope(child.id, node)
                if scope is None or (scope, child.id) in seen:
                    continue
                for value in self.values[(scope, child.id)]:
                    parts.append(self.text(value, node, seen | {(scope, child.id)}))
        return "\n".join(parts)


def _callee_names(call: ast.Call, bindings: _Bindings) -> set[str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        return {func.attr}
    if isinstance(func, ast.Name):
        return {func.id} | bindings.alias_names(func.id, call)
    named = _getattr_name(func)
    return {named} if named is not None else set()


def _shape_of(call: ast.Call, bindings: _Bindings) -> str | None:
    names = _callee_names(call, bindings)
    if "transaction" in names:
        return "transaction"
    if "rollback" in names:
        return "rollback"
    if any("execute" in name for name in names) and call.args:
        if ROLLS_BACK_BY_SQL.search(bindings.text(call.args[0], call)):
            return "sql"
    return None


def _statement_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.stmt:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(current)
    assert isinstance(current, ast.stmt)
    return current


def _names_called_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                names.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
    return names


def _has_comment(lines: list[str], line: int) -> bool:
    """The marker on ``line`` itself or in the comment lines directly above it."""

    if MARKER in lines[line - 1]:
        return True
    above = line - 1
    while above >= 1 and lines[above - 1].lstrip().startswith("#"):
        if MARKER in lines[above - 1]:
            return True
        above -= 1
    return False


def _inside(node: ast.AST, container: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if current is container:
            return True
        current = parents.get(current)
    return False


def _handlers_empty_the_state(statement: ast.AST) -> bool:
    return any(EMPTIES_THE_STATE & _names_called_in(handler) for handler in getattr(statement, "handlers", []))


def _wrapped(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """A ``drop_label_state_on_rollback`` item ahead of the block, or a handler that empties the state."""

    current = parents.get(node)
    while current is not None and not isinstance(current, FUNCTIONS):
        if isinstance(current, (ast.With, ast.AsyncWith)):
            for index, item in enumerate(current.items):
                if WRAPPER not in _names_called_in(item.context_expr):
                    continue
                # The wrapper empties the state when the block raises, so the block has to be after it, not in it.
                at_or_before_the_wrapper = any(
                    _inside(node, other.context_expr, parents) for other in current.items[: index + 1]
                )
                if not at_or_before_the_wrapper:
                    return True
        if isinstance(current, TRY_STATEMENTS) and _handlers_empty_the_state(current):
            return True
        current = parents.get(current)
    return False


def _opens_a_savepoint_for_a_handler(statement: ast.stmt, parents: dict[ast.AST, ast.AST]) -> bool:
    """The statement just before a ``try`` whose handler empties the state: ``SAVEPOINT x`` then ``try: ... except``."""

    parent = parents.get(statement)
    for field in ("body", "orelse", "finalbody"):
        siblings = getattr(parent, field, None)
        if isinstance(siblings, list) and statement in siblings:
            index = siblings.index(statement)
            following = siblings[index + 1] if index + 1 < len(siblings) else None
            return isinstance(following, TRY_STATEMENTS) and _handlers_empty_the_state(following)
    return False


def scan_source(text: str, file: str = "<source>") -> list[Site]:
    """Every block of ``text`` that can roll back, and whether it is accounted for on its own."""

    tree = ast.parse(text)
    lines = text.splitlines()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    bindings = _Bindings(tree, parents)
    found: list[Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        shape = _shape_of(node, bindings)
        if shape is None:
            continue
        statement = _statement_of(node, parents)
        holder = _scope_of(node, parents)
        accounted = (
            _has_comment(lines, node.lineno)
            or _has_comment(lines, statement.lineno)
            or _wrapped(node, parents)
            or _opens_a_savepoint_for_a_handler(statement, parents)
        )
        name = "<module>" if isinstance(holder, ast.Module) else getattr(holder, "name", "<lambda>")
        found.append(Site(file, node.lineno, name, shape, accounted))
    return sorted(found, key=lambda site: site.line)


def rollback_sites() -> list[Site]:
    """Every block of the product code that can roll back."""

    found: list[Site] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if "/migrations/" in relative:
            continue
        found.extend(scan_source(path.read_text(encoding="utf-8"), relative))
    return found


def test_the_scan_finds_the_blocks_it_is_meant_to_find() -> None:
    sites = {(site.file.rsplit("/", 1)[-1], site.function) for site in rollback_sites()}
    assert ("vnext_label_writes.py", "capture_label_inputs") in sites
    assert ("vnext_embeddings.py", "_embedding_write_savepoint") in sites
    assert ("vnext_store.py", "savepoint") in sites
    assert ("sqlite_store.py", "savepoint") in sites
    assert ("providers.py", "_persist_discovered_provider_capability") in sites


def test_every_block_that_can_roll_back_says_what_happens_to_the_label_state() -> None:
    unaccounted = [f"{site.file}:{site.line} in {site.function} ({site.shape})" for site in rollback_sites() if not site.accounted]
    assert unaccounted == [], (
        "These blocks can roll back and neither empty the request's label state nor say why they need not. "
        "Put a '# label state: <reason>' comment on the block or just above it, or wrap the block in "
        "drop_label_state_on_rollback. A comment elsewhere in the function does not cover it:\n" + "\n".join(unaccounted)
    )


# --- the scan itself, on source text that is not part of the product -------------------------------------------


def _unaccounted(source: str) -> list[tuple[int, str]]:
    return [(site.line, site.shape) for site in scan_source(textwrap.dedent(source)) if not site.accounted]


def _all(source: str) -> list[tuple[int, str]]:
    return [(site.line, site.shape) for site in scan_source(textwrap.dedent(source))]


def test_a_plain_transaction_without_a_decision_fails_and_with_one_passes() -> None:
    assert _unaccounted(
        """
        def persist(conn):
            with conn.transaction():
                conn.execute("INSERT INTO t VALUES (1)")
        """
    ) == [(3, "transaction")]
    assert _unaccounted(
        """
        def persist(conn):
            # label state: none, this table carries no label.
            with conn.transaction():
                conn.execute("INSERT INTO t VALUES (1)")
        """
    ) == []
    assert _unaccounted(
        """
        def persist(conn):
            with conn.transaction():  # label state: none, this table carries no label.
                conn.execute("INSERT INTO t VALUES (1)")
        """
    ) == []


def test_an_aliased_transaction_fails() -> None:
    """``begin = store.conn.transaction`` hides the word from a search for ``.transaction(``."""

    assert _unaccounted(
        """
        def persist(store):
            begin = store.conn.transaction
            with begin():
                store.write()
        """
    ) == [(4, "transaction")]


@pytest.mark.parametrize(
    "binding",
    [
        'begin = getattr(store.conn, "transaction", None)',
        "begin = store.conn.transaction if store.conn else None",
        "begin = store.conn and store.conn.transaction",
    ],
)
def test_an_alias_made_with_getattr_or_a_condition_fails(binding: str) -> None:
    source = f"""
        def persist(store):
            {binding}
            with begin():
                store.write()
        """
    assert _unaccounted(source) == [(4, "transaction")]


def test_an_aliased_transaction_with_its_own_decision_passes() -> None:
    assert _unaccounted(
        """
        def persist(store):
            begin = store.conn.transaction
            # label state: none, this block only reads.
            with begin():
                store.read()
        """
    ) == []


def test_an_alias_bound_in_another_function_does_not_count() -> None:
    """The alias is looked up in the function that calls it and the ones around it, nowhere else."""

    assert _all(
        """
        def elsewhere(store):
            begin = store.conn.transaction

        def persist(begin):
            with begin():
                pass
        """
    ) == []


def test_sql_held_in_a_variable_fails() -> None:
    assert _unaccounted(
        """
        def undo(conn):
            sql = 'ROLLBACK'
            conn.execute(sql)
        """
    ) == [(4, "sql")]
    assert _unaccounted(
        """
        def undo(conn):
            statement = f"ROLLBACK TO SAVEPOINT {conn.name}"
            cur = conn.cursor()
            cur.execute(statement)
        """
    ) == [(5, "sql")]


def test_sql_in_a_module_constant_or_a_joined_string_fails() -> None:
    assert _unaccounted(
        """
        UNDO = "rollback"

        def undo(conn):
            conn.execute(UNDO)
        """
    ) == [(5, "sql")]
    assert _unaccounted(
        """
        def undo(conn):
            prefix = "SELECT 1; "
            conn.execute(prefix + "ROLLBACK;")
        """
    ) == [(4, "sql")]


def test_sql_in_a_variable_with_its_own_decision_passes() -> None:
    assert _unaccounted(
        """
        def undo(conn):
            sql = "ROLLBACK"
            # label state: none, this connection has no store.
            conn.execute(sql)
        """
    ) == []


def test_an_execute_that_does_not_roll_back_is_not_a_block() -> None:
    assert _all(
        """
        def read(conn, rollback_count):
            sql = "SELECT rollback_count FROM t"
            conn.execute(sql)
            conn.execute("SELECT 1")
            print("could not ROLLBACK")
            return conn.cursor().fetchall()
        """
    ) == []


def test_an_execute_reached_through_an_alias_fails() -> None:
    assert _unaccounted(
        """
        def undo(conn):
            run = getattr(conn, "execute", None)
            run("ROLLBACK TO SAVEPOINT x")
        """
    ) == [(4, "sql")]


def test_a_rollback_call_without_a_decision_fails() -> None:
    assert _unaccounted(
        """
        def undo(conn):
            conn.rollback()
        """
    ) == [(3, "rollback")]


def test_a_second_bare_block_in_a_function_with_a_comment_fails() -> None:
    """The earlier scan judged the function, so one comment covered every block in it."""

    assert _unaccounted(
        """
        def persist(conn):
            # label state: none, this block only reads.
            with conn.transaction():
                conn.execute("SELECT 1")
            with conn.transaction():
                conn.execute("INSERT INTO t VALUES (1)")
        """
    ) == [(6, "transaction")]


def test_a_second_bare_block_in_a_function_with_a_wrapper_fails() -> None:
    assert _unaccounted(
        """
        def persist(store, conn):
            with drop_label_state_on_rollback(store), conn.transaction():
                store.write()
            with conn.transaction():
                store.write()
        """
    ) == [(5, "transaction")]


def test_a_second_bare_block_after_a_helper_call_in_the_function_fails() -> None:
    assert _unaccounted(
        """
        def persist(store, conn):
            with conn.transaction():  # label state: none, this block only reads.
                store.read()
            invalidate_read_labels(store)
            with conn.transaction():
                store.write()
        """
    ) == [(6, "transaction")]


def test_a_comment_further_up_than_the_line_above_does_not_count() -> None:
    assert _unaccounted(
        """
        def persist(conn):
            # label state: none, this block only reads.
            conn.execute("SELECT 1")
            with conn.transaction():
                conn.execute("SELECT 1")
        """
    ) == [(5, "transaction")]


def test_a_comment_in_a_block_above_the_statement_counts_when_the_marker_is_in_it() -> None:
    assert _unaccounted(
        """
        def persist(conn):
            # label state: none, this block only reads, and the
            # connection is closed right after it.
            with conn.transaction():
                conn.execute("SELECT 1")
        """
    ) == []


def test_a_comment_above_a_statement_that_spans_several_lines_counts_for_the_block_inside_it() -> None:
    assert _unaccounted(
        """
        def persist(conn, other):
            # label state: none, both only read.
            with (
                other.thing(),
                conn.transaction(),
            ):
                conn.execute("SELECT 1")
        """
    ) == []


def test_the_wrapper_has_to_come_before_the_transaction() -> None:
    assert _unaccounted(
        """
        def persist(store, conn):
            with conn.transaction(), drop_label_state_on_rollback(store):
                store.write()
        """
    ) == [(3, "transaction")]
    assert _unaccounted(
        """
        def persist(store, conn):
            with drop_label_state_on_rollback(store):
                with conn.transaction():
                    store.write()
        """
    ) == []


def test_a_wrapper_in_another_function_does_not_cover_a_nested_one() -> None:
    assert _unaccounted(
        """
        def outer(store, conn):
            with drop_label_state_on_rollback(store):
                def inner():
                    with conn.transaction():
                        store.write()
                return inner
        """
    ) == [(5, "transaction")]


def test_a_savepoint_whose_try_empties_the_state_passes_and_one_whose_try_does_not_fails() -> None:
    assert _unaccounted(
        """
        def savepoint(self):
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                label_savepoint_rolled_back(self)
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                raise
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
        """
    ) == []
    assert _unaccounted(
        """
        def savepoint(self):
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                raise
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
        """
    ) == [(3, "sql"), (7, "sql"), (10, "sql")]


def test_a_savepoint_that_is_not_followed_by_a_try_that_empties_the_state_fails() -> None:
    assert _unaccounted(
        """
        def savepoint(self):
            self.conn.execute(f"SAVEPOINT {name}")
            self.write()
            try:
                yield
            except BaseException:
                invalidate_read_labels(self)
                raise
        """
    ) == [(3, "sql")]


def test_every_block_of_a_function_is_listed_with_its_own_line() -> None:
    sites = scan_source(
        textwrap.dedent(
            """
            def persist(conn):
                # label state: none, reads only.
                with conn.transaction():
                    pass
                with conn.transaction():
                    pass
                conn.rollback()
            """
        )
    )
    assert [(site.line, site.shape, site.accounted) for site in sites] == [
        (4, "transaction", True),
        (6, "transaction", False),
        (8, "rollback", False),
    ]


def test_the_scan_of_the_product_code_sees_transactions_and_statements() -> None:
    shapes = {site.shape for site in rollback_sites()}
    assert {"transaction", "sql"} <= shapes
