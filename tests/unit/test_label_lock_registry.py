"""Discover every store SQL write or row lock on a label table."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
TABLE = r"(?:memories|sources|open_loops|generated_artifacts|projects)"
WRITE = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{TABLE}\b", re.I)
FROM = re.compile(rf"\b(?:FROM|JOIN)\s+{TABLE}\b", re.I)


def sql_text(node):
    if isinstance(node, ast.JoinedStr):
        return " ".join(item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else "{}" for item in node.values)
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def label_sql_functions(tree):
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            query = " ".join(sql_text(node).split())
            if WRITE.search(query) or ("FOR UPDATE" in query.upper() and FROM.search(query)):
                yield fn, node
                break


def test_all_discovered_label_writers_and_row_lockers_take_l():
    paths = [ROOT / "vnext_store.py", *sorted((ROOT / "vnext_stores/postgres").glob("*.py"))]
    found = set()
    missing = []
    for path in paths:
        for fn, node in label_sql_functions(ast.parse(path.read_text())):
            found.add((path.name, fn.name))
            if not any(isinstance(dec, ast.Name) and dec.id == "takes_label_lock" for dec in fn.decorator_list):
                missing.append(f"{path.name}:{node.lineno} {fn.name}")
    assert ("memory_access.py", "list_pending_derived_candidates_for_member") in found
    assert ("memory_lifecycle.py", "lock_project_update_artifacts_for_redaction") in found
    assert not missing, "label SQL without L: " + ", ".join(missing)


# Each application SQL exception has a dedicated transaction protocol.
DIRECT_SQL_PROTOCOLS = {
    ("vnext_label_writes.py", "write_settled_label"): "exclusive L and label compare-and-set",
    ("vnext_label_writes.py", "lock_settled_label_rows"): "exclusive L and deterministic table order",
    ("vnext_label_repair.py", "relabel_labels_sqlite"): "SQLite immediate writer transaction",
    ("vnext_derived_domain_backfill.py", "relabel_derived_rows_sqlite"): "frozen historical SQLite repair",
    ("labels.py", "repair_labels_postgres"): "S, exclusive L, ordered rows and compare-and-set",
    ("sqlite_schema.py", "_backfill_legacy_memory_project_scopes"): "schema bootstrap writer transaction",
    ("sqlite_schema.py", "_backfill_source_dedupe_keys"): "schema bootstrap writer transaction",
    ("sqlite_schema.py", "_repair_source_dedupe_identity"): "schema bootstrap writer transaction",
    ("sqlite_schema.py", "_backfill_memory_agent_attribution"): "schema bootstrap writer transaction",
    ("sqlite_schema.py", "_deduplicate_memory_lookup_values"): "schema bootstrap writer transaction",
    ("sqlite_schema.py", "_repair_tombstone_lookup_value_holders"): "schema bootstrap writer transaction",
}


def test_application_code_cannot_write_label_tables_directly():
    missing = []
    for path in ROOT.rglob("*.py"):
        if path.name in {"vnext_store.py", "sqlite_store.py", "store.py"} or "vnext_stores" in path.parts or "legacy_store" in path.parts:
            continue
        for fn, node in label_sql_functions(ast.parse(path.read_text())):
            if any(isinstance(dec, ast.Name) and dec.id in {"takes_label_lock", "_takes_label_lock"} for dec in fn.decorator_list):
                continue
            if (path.name, fn.name) not in DIRECT_SQL_PROTOCOLS:
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {fn.name}")
    assert not missing, "application label SQL without protocol: " + ", ".join(missing)
