"""Temporary measurement: reader scan cost of an advised matrix vs one realloc made (Linux only).

Builds two matrices of about 170,000 rows of 768 float32, one with np.empty at the full size
(numpy advises madvise(MADV_HUGEPAGE) on it) and one small then grown by ndarray.resize (realloc
made, no advice), fills both, and times the scoring scan of the reader over each, interleaved.
"""
import statistics
import time
import sys

import numpy as np

from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import _ROWS_PER_STEP

rows, dims, runs = 170_000, 768, 24
rng = np.random.default_rng(5)


def make(advised):
    if advised:
        matrix = np.empty((rows, dims), dtype=np.float32)
    else:
        matrix = np.empty((1, dims), dtype=np.float32)
        matrix.resize((rows, dims), refcheck=False)
    for start in range(0, rows, _ROWS_PER_STEP):
        stop = min(start + _ROWS_PER_STEP, rows)
        matrix[start:stop] = rng.random((stop - start, dims), dtype=np.float32)
    return matrix


def flags_of(ptr):
    with open("/proc/self/smaps") as fh:
        for line in fh:
            if "-" not in line or ":" not in line.split()[0]:
                continue
            low, high = (int(part, 16) for part in line.split()[0].split("-"))
            if low <= ptr < high:
                for inner in fh:
                    if inner.startswith("VmFlags"):
                        return inner.strip()
                    if "-" in inner.split()[0] and ":" in inner.split()[0]:
                        break
    return "mapping not found"


advised_matrix = make(True)
grown_matrix = make(False)
print("advised  :", hex(advised_matrix.ctypes.data), flags_of(advised_matrix.ctypes.data))
print("realloc  :", hex(grown_matrix.ctypes.data), flags_of(grown_matrix.ctypes.data))

query = rng.random(dims).astype(np.float32)


def scan(matrix):
    dots_sum = 0.0
    for start in range(0, rows, _ROWS_PER_STEP):
        stop = min(start + _ROWS_PER_STEP, rows)
        dots_sum += float((matrix[start:stop] @ query).sum())
    return dots_sum


assert abs(scan(advised_matrix) - scan(grown_matrix)) < 1e6  # same work, sanity only
timings = {"advised": [], "realloc": []}
for _ in range(runs):
    for name, matrix in (("advised", advised_matrix), ("realloc", grown_matrix)):
        start = time.perf_counter()
        scan(matrix)
        timings[name].append(time.perf_counter() - start)

for name in ("advised", "realloc"):
    values = sorted(timings[name])
    print(
        f"{name}: p50={statistics.quantiles(values, n=10)[4]:.5f}s "
        f"p90={statistics.quantiles(values, n=10)[8]:.5f}s min={values[0]:.5f}s n={len(values)}"
    )
