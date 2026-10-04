"""Reentrant file locks for local vault sidecars, shared across processes."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

_REGISTRY_GUARD = threading.Lock()
_LOCKS: dict[str, object] = {}
_HELD = threading.local()


@contextmanager
def vault_file_lock(path: Path):
    key = str(path.resolve())
    with _REGISTRY_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:  # type: ignore[attr-defined]
        held: set[str] = getattr(_HELD, 'paths', set())
        if key in held:
            yield
            return
        guard = path.with_name(path.name + '.lock')
        descriptor = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b'\0')
                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(msvcrt, 'locking')(descriptor, getattr(msvcrt, 'LK_LOCK'), 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            _HELD.paths = held
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                if os.name == 'nt':
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    getattr(msvcrt, 'locking')(descriptor, getattr(msvcrt, 'LK_UNLCK'), 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
