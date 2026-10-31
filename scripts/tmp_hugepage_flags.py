"""Temporary measurement: VmFlags of the mapping holding the cache matrix (Linux only)."""
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

user = "00000000-0000-0000-0000-000000000001"
signature = ChunkSignature("p", "m", "e", 1, 1, 0)
rng = np.random.default_rng(7)
tmp = Path(sys.argv[1])
tmp.mkdir(parents=True, exist_ok=True)
database = tmp / "flags.db"
bootstrap_database(database, user_id=UUID(user), user_email="local@alice")

conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")


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


insert(0, 3000)
conn.close()


def flags_of(ptr):
    with open("/proc/self/smaps") as fh:
        for line in fh:
            if "-" not in line or ":" not in line.split()[0]:
                continue
            span = line.split()[0]
            low, high = (int(part, 16) for part in span.split("-"))
            if low <= ptr < high:
                for inner in fh:
                    if inner.startswith("VmFlags"):
                        return inner.strip()
                    if "-" in inner.split()[0] and ":" in inner.split()[0]:
                        break
    return "mapping not found"


def open_and_note(expect_rows, note):
    with sqlite_user_connection(database, user) as c:
        with chunk_vector_view(c, user, signature) as view:
            assert view.total_rows == expect_rows, (view.total_rows, expect_rows)
            entry = chunk_vector_cache._REGISTRY[(str(database), user)]
            print(f"{note}: rows={view.total_rows} ptr={entry.matrix.ctypes.data:#x} {flags_of(entry.matrix.ctypes.data)}")


open_and_note(3000, "after build")
conn = sqlite3.connect(database)
conn.execute("PRAGMA foreign_keys=OFF")
insert(3000, 500)
conn.close()
open_and_note(3500, "after append")

plain = np.empty((3000, 768), dtype=np.float32)
print(f"direct np.empty((3000, 768)): ptr={plain.ctypes.data:#x} {flags_of(plain.ctypes.data)}")
