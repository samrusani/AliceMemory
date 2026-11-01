"""Temporary measurement: append peak and advice flags in a long lived process (Linux only).

One process first allocates and frees numpy arrays of several sizes between 4 and 32 MiB, so the allocator's
dynamic mmap threshold rises and advised regions exist, then builds a resident copy of 30,000 vectors of 768
float32 and appends 8,000. Reports the rise of the peak resident size over the append and the VmFlags of every
mapping that overlaps the matrix, after the build and after the append.
"""
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import numpy as np

from alicebot_api.onramp import bootstrap_database
from alicebot_api.source_embedding_policy import ChunkSignature
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_stores.sqlite import chunk_vector_cache
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import chunk_vector_view

database, user = Path(sys.argv[1]), sys.argv[2]
signature = ChunkSignature("p", "m", "e", 1, 1, 0)
rng = np.random.default_rng(7)
bootstrap_database(database, user_id=UUID(user), user_email="local@alice")
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
from alicebot_api.sqlite_schema import ensure_source_chunk_vectors_table  # noqa: E402

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


def flags_over(pointer, size):
    found = []
    with open("/proc/self/smaps") as mapping_file:
        hold = None
        for line in mapping_file:
            head = line.split()
            if len(head) < 1 or "-" not in head[0] or ":" in head[0]:
                if hold is not None and line.startswith("VmFlags"):
                    found.append(line.split(":")[1].strip())
                continue
            low, high = (int(part, 16) for part in head[0].split("-"))
            hold = low if low < pointer + size and high > pointer else None
    return found


def hwm():
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmHWM"):
                return int(line.split()[1]) * 1024


# Warm up like a server that has served large arrays: several sizes between 4 and 32 MiB, allocated and freed,
# twice, so the dynamic mmap threshold rises and advised regions exist.
for round_index in range(2):
    for mebibytes in (4, 8, 16, 24, 32):
        warm = np.empty((mebibytes * 1024 * 1024) // 4, dtype=np.float32)
        warm[:] = 1.0
        del warm

insert(0, 30000)
conn.close()
with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        built = view.total_rows
        entry = chunk_vector_cache._REGISTRY[(str(database), user)]
        flags_built = flags_over(entry.matrix.ctypes.data, entry.matrix.nbytes)
        hwm_built = hwm()
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
ensure_source_chunk_vectors_table(conn)
insert(30000, 8000)
conn.close()
with sqlite_user_connection(database, user) as c:
    with chunk_vector_view(c, user, signature) as view:
        grown = view.total_rows
        entry = chunk_vector_cache._REGISTRY[(str(database), user)]
        flags_grown = flags_over(entry.matrix.ctypes.data, entry.matrix.nbytes)
after = hwm()
matrix = 30000 * 768 * 4
print(json.dumps({
    "built": built, "grown": grown, "matrix": matrix,
    "rise": after - hwm_built, "share": round((after - hwm_built) / matrix, 4),
    "advised_built": any("hg" in line for line in flags_built),
    "advised_grown": any("hg" in line for line in flags_grown),
    "flags_built": flags_built, "flags_grown": flags_grown,
}))
