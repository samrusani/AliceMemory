"""Instrumented append probe. Usage: probe_instrumented.py DATABASE USER FIRST MORE MODE
Modes: base (steps), hugepage_off (env only handled by caller), single (one resize to final),
late_before (baseline read just before _append)."""
import json, resource, sqlite3, sys
import numpy as np
from alicebot_api.sqlite_schema import ensure_source_chunk_vectors_table
from alicebot_api.source_embedding_policy import ChunkSignature
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_stores.sqlite import chunk_vector_cache
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import chunk_vector_view

database, user, first_rows, more_rows, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
from pathlib import Path
from alicebot_api.onramp import resolve_db_path, bootstrap_database
from uuid import UUID
Path(database).parent.mkdir(parents=True, exist_ok=True)
bootstrap_database(Path(database), user_id=UUID(user), user_email="local@alice")
signature = ChunkSignature("p", "m", "e", 1, 1, 0)
rng = np.random.default_rng(7)
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
ensure_source_chunk_vectors_table(conn)

def insert(start, count):
    for low in range(start, start + count, 1000):
        high = min(low + 1000, start + count)
        block = rng.random((high - low, 768), dtype=np.float32)
        conn.executemany(
            "INSERT INTO source_chunk_embeddings (chunk_id, user_id, source_id, dimensions, embedding, provider, model, "
            "endpoint, signature_version, header_version, prefix_version, content_sha256) "
            "VALUES (?, ?, ?, 768, ?, 'p', 'm', 'e', 1, 1, 0, 'x')",
            [(f"c{low + i}", user, f"s{(low + i) % 50}", block[i].tobytes()) for i in range(high - low)],
        )
    conn.commit()

insert(0, first_rows)
conn.close()
with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        built = view.total_rows

conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
insert(first_rows, more_rows)
conn.close()

scale = 1 if sys.platform == "darwin" else 1024
if mode != "late_before":
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale

resize_log = []
def _log_resize(arr, newshape, tag):
    rec = {"tag": tag, "obj": id(arr), "ptr_before": arr.ctypes.data, "shape_before": list(arr.shape),
           "nbytes": int(arr.nbytes), "newshape": list(newshape)}
    arr.resize(newshape, refcheck=False)
    rec["ptr_after"] = arr.ctypes.data
    rec["moved"] = rec["ptr_before"] != rec["ptr_after"]
    rec["maxrss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    resize_log.append(rec)

# Clone of _append with logging at each resize (np.ndarray.resize cannot be patched).
from alicebot_api.sqlite_schema import source_chunk_vectors_table_exists
_read_blob_into = chunk_vector_cache._read_blob_into
_norms_of = chunk_vector_cache._norms_of
_signature_params = chunk_vector_cache._signature_params
def _logged_append(entry, conn, uid, sig, max_bytes):
    if not source_chunk_vectors_table_exists(conn):
        return
    newest = int(chunk_vector_cache._query_rows(conn, f"SELECT COALESCE(MAX(rowid), 0) FROM source_chunk_embeddings", ()).fetchone()[0])
    if newest <= entry.max_rowid:
        return
    params = _signature_params(sig)
    entry.appends += 1
    while entry.max_rowid < newest:
        cursor = chunk_vector_cache._query_rows(
            conn,
            "SELECT rowid, chunk_id, source_id, dimensions, embedding, "
            "(provider = ? AND model = ? AND endpoint = ? AND signature_version = ? AND header_version = ? AND prefix_version = ?) AS sig_ok "
            "FROM source_chunk_embeddings WHERE user_id = ? AND rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
            (*params, uid, entry.max_rowid, newest, chunk_vector_cache._ROWS_PER_STEP))
        page = cursor.fetchall()
        cursor.close()
        if not page:
            entry.max_rowid = newest
            break
        entry.max_rowid = int(page[-1][0])
        fresh = []
        for row in page:
            if row[5] and entry.dims in (0, int(row[3])):
                fresh.append(row)
            else:
                entry.stale_codes.append(entry.code_for(str(row[2])))
        if not fresh:
            continue
        if entry.dims == 0:
            entry.dims = int(fresh[0][3])
            entry.matrix = np.empty((0, entry.dims), dtype=np.float32)
        start = entry.rows
        if (start + len(fresh)) * chunk_vector_cache._row_bytes(entry.dims) > max_bytes:
            entry.reset()
            raise chunk_vector_cache.ChunkVectorCacheRefused("over_cap")
        _log_resize(entry.matrix, (start + len(fresh), entry.dims), "matrix")
        _log_resize(entry.codes, (start + len(fresh),), "codes")
        kept = 0
        for _rowid, chunk_id, source_id, _dims, blob, _ok in fresh:
            if not _read_blob_into(entry.matrix[start + kept], blob, entry.dims):
                continue
            code = entry.code_for(str(source_id))
            entry.codes[start + kept] = code
            entry.chunk_ids.append(str(chunk_id))
            entry.rows_of_source.setdefault(code, []).append(start + kept)
            kept += 1
        if kept != len(fresh):
            _log_resize(entry.matrix, (start + kept, entry.dims), "matrix-shrink")
            _log_resize(entry.codes, (start + kept,), "codes-shrink")
        tail = entry.matrix[start : start + kept]
        _log_resize(entry.norms, (start + kept,), "norms")
        entry.norms[start : start + kept] = _norms_of(tail)
chunk_vector_cache._append = _logged_append

append_log = []
_inner_append = chunk_vector_cache._append
def _snap_append(entry, conn, uid, sig, max_bytes):
    counters = {"rebuilds": entry.rebuilds, "appends": entry.appends, "rows": entry.rows, "dims": entry.dims, "bytes": entry.resident_bytes}
    append_log.append({"phase": "append_start", "matrix_obj": id(entry.matrix), "ptr": entry.matrix.ctypes.data,
                       "rows": entry.matrix.shape[0], "counters": counters,
                       "maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale})
    result = _inner_append(entry, conn, uid, sig, max_bytes)
    counters2 = {"rebuilds": entry.rebuilds, "appends": entry.appends, "rows": entry.rows, "dims": entry.dims, "bytes": entry.resident_bytes}
    append_log.append({"phase": "append_end", "matrix_obj": id(entry.matrix), "ptr": entry.matrix.ctypes.data,
                       "rows": entry.matrix.shape[0], "counters": counters2,
                       "maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale})
    return result
chunk_vector_cache._append = _snap_append

if mode == "single":
    # one resize to the final size instead of steps: patch _ROWS_PER_STEP so one page holds all rows
    chunk_vector_cache._ROWS_PER_STEP = more_rows + 1000

if mode == "late_before":
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    before_snapshot = None
    # patch again just to capture right before append (append_log first entry has it)

with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        grown = view.total_rows
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale

def vm():
    out = {}
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith(("VmRSS", "VmHWM", "VmData")):
                    k, v = line.split(":")
                    out[k] = v.strip()
        with open("/proc/self/smaps_rollup") as fh:
            for line in fh:
                if line.startswith(("RssAnon", "RssFile")):
                    k, v = line.split(":")
                    out[k] = v.strip()
    except OSError:
        out["note"] = "no /proc on this platform"
    return out

print(json.dumps({"built": built, "grown": grown, "before": before, "after": after,
                  "delta": after - before, "matrix": first_rows * 768 * 4,
                  "mode": mode, "vm": vm(), "appends": append_log, "resizes": resize_log}))
