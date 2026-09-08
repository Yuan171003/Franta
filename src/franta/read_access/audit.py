"""Append-only audit records for project-memory and task reads."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class AuditLog:
    """Append-only JSONL audit sink."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, event: Mapping[str, Any]) -> None:
        value = {
            "time": datetime.now(timezone.utc).isoformat(),
            **dict(event),
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.events.append(value)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())


__all__ = ["AuditLog"]
