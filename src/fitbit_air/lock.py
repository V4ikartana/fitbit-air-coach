"""A kernel file lock so a scheduled sync and a manual sync never run at the same time.

The lock is an flock(2) on the lock file. The kernel drops it if the process dies or the Mac
reboots, so no staleness logic is needed. The PID written into the file is for diagnostics only.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class SyncLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.held = False
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:  # any other failure must not leak the descriptor
            os.close(fd)
            raise
        self._fd = fd
        self.held = True
        return True

    def release(self) -> None:
        if self.held and self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the lock; the file stays
            self._fd = None
            self.held = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
