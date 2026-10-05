"""Discover label-changing SQL, update patches and the derived insert floors."""
import ast
import re
from pathlib import Path

from tests.unit.test_label_lock_registry import ROOT, label_sql_functions, sql_text

LABEL_KEYS = {"domain", "sensitivity", "project_id", "project_scope", "project_floor", "derived_from"}
UPDATE_METHODS = {"update_memory", "update_source", "update_open_loop", "update_project"}
# Explicit function identities make every newly added caller reviewable.
LABEL_CALLERS = {
    ("routers/vnext_memories.py", "review_vnext_memory"),
    ("routers/vnext_memories.py", "review_vnext_source"),
    ("vnext_projects.py", "review_project_update"),
    ("vnext_label_writes.py", "raise_source_to_replacement"),
    ("sqlite_store.py", "supersede_source"),
    ("cli/smokes.py", "_run_vnext_smoke_operator_console"),
}


def keys_in(node):
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)} & LABEL_KEYS


def test_every_label_changing_update_caller_is_registered():
    missing = []
    for path in ROOT.rglob("*.py"):
        if "vnext_stores" in path.parts or path.name in {"sqlite_store.py", "vnext_store.py"}:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            patch_nodes = [node for node in ast.walk(fn) if isinstance(node, (ast.Assign, ast.AnnAssign)) and any(isinstance(item, ast.Name) and "patch" in item.id for item in ast.walk(node))]
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr not in UPDATE_METHODS:
                    continue
                patch = next((kw.value for kw in call.keywords if kw.arg == "patch"), None)
                if patch is not None and (keys_in(patch) or (isinstance(patch, ast.Name) and any(keys_in(item) for item in patch_nodes))):
                    key = (str(path.relative_to(ROOT)), fn.name)
                    if key not in LABEL_CALLERS:
                        missing.append(f"{key[0]}:{call.lineno} {fn.name}")
    assert not missing, "unregistered label caller: " + ", ".join(missing)


def test_every_create_has_the_insert_floor_and_every_update_has_its_hook():
    floors = {
        "vnext_store.py": {"create_artifact", "upsert_artifact_by_workflow_digest", "create_project", "update_project"},
        "vnext_stores/postgres/memory_lifecycle.py": {"create_memory"},
        "vnext_stores/postgres/graph_open_loops.py": {"create_open_loop"},
        "vnext_stores/sqlite/memory_lifecycle.py": {"create_memory"},
        "vnext_stores/sqlite/graph_open_loops.py": {"create_open_loop"},
    }
    hooks = {
        "vnext_store.py": {"update_source", "update_project"},
        "sqlite_store.py": {"update_source"},
        "vnext_stores/postgres/memory_lifecycle.py": {"update_memory"},
        "vnext_stores/postgres/graph_open_loops.py": {"update_open_loop"},
        "vnext_stores/sqlite/memory_lifecycle.py": {"update_memory"},
        "vnext_stores/sqlite/graph_open_loops.py": {"update_open_loop"},
    }
    for registry, helper in ((floors, "apply_insert_floor"), (hooks, "propagate_after_write")):
        for name, functions in registry.items():
            tree = ast.parse((ROOT / name).read_text())
            for function in functions:
                fn = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function)
                calls = {node.func.id for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
                assert helper in calls, f"{name}:{fn.lineno} {function} lacks {helper}"


def test_a_new_derived_insert_cannot_bypass_the_floor():
    for path in [ROOT / "vnext_store.py", ROOT / "sqlite_store.py", *sorted((ROOT / "vnext_stores").rglob("*.py"))]:
        for fn, node in label_sql_functions(ast.parse(path.read_text())):
            inserts = [sql_text(item) for item in ast.walk(fn)]
            if not any(re.search(r"\bINSERT\s+INTO\s+(?:memories|open_loops|generated_artifacts|projects)\b", query, re.I) for query in inserts):
                continue
            calls = {item.func.id for item in ast.walk(fn) if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)}
            assert "apply_insert_floor" in calls, f"{path.name}:{node.lineno} {fn.name} inserts without a floor"
