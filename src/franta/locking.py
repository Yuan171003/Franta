"""Single-scheduler process lock."""

from __future__ import annotations

from pathlib import Path
import fcntl
import os
from types import TracebackType

from .contracts.failures import FrantaError


class SchedulerAlreadyRunning(FrantaError):
    pass


class ProjectLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise SchedulerAlreadyRunning(f"scheduler already owns {self.path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    def __enter__(self) -> "ProjectLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()
