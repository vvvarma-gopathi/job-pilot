from __future__ import annotations

import os
from pathlib import Path


class AgentAlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise AgentAlreadyRunning("A JobPilot agent instance already holds the process lock") from exc
        self._handle.seek(1)
        self._handle.truncate()
        self._handle.write(str(os.getpid()).encode("ascii"))
        self._handle.flush()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    @classmethod
    def is_locked(cls, path: str | Path) -> bool:
        try:
            with cls(path):
                return False
        except AgentAlreadyRunning:
            return True
