"""Independent, evidence-bound research summaries with a durable display cache."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Mapping

from .interfaces import DashboardReadPort, MonitorPort


QUALITY_DIMENSIONS = (
    "creativity", "synthesis", "critical_obstacles", "breakthrough_potential",
    "proof_closure_potential",
)


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def validate_summary(value: Any, source_ids: set[str], *, require_quality: bool = False) -> dict[str, Any]:
    """Accept at most five complete recommendations citing the supplied snapshot."""

    if not isinstance(value, Mapping) or not isinstance(value.get("directions"), list):
        raise ValueError("Monitor result must contain a directions list")
    if len(value["directions"]) > 5:
        raise ValueError("Monitor may return at most five research directions")
    directions = []
    for item in value["directions"]:
        if not isinstance(item, Mapping):
            raise ValueError("Monitor directions must be objects")
        direction = {}
        for field in ("title", "summary", "why_promising", "obstacles", "next_step"):
            text = item.get(field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Monitor direction requires nonempty {field}")
            direction[field] = text
        references = item.get("source_ids")
        if (
            not isinstance(references, list)
            or not references
            or any(not isinstance(ref, str) or ref not in source_ids for ref in references)
        ):
            raise ValueError("Monitor direction must cite existing snapshot source IDs")
        direction["source_ids"] = list(dict.fromkeys(references))
        directions.append(direction)
    result = {"directions": directions}
    if "quality" in value or require_quality:
        quality = value.get("quality")
        if not isinstance(quality, Mapping) or set(quality) != set(QUALITY_DIMENSIONS):
            raise ValueError("Monitor quality must contain all five assessment dimensions")
        assessments = {}
        for dimension in QUALITY_DIMENSIONS:
            item = quality[dimension]
            if not isinstance(item, Mapping):
                raise ValueError("Monitor quality assessments must be objects")
            score = item.get("score")
            if type(score) is not int or not 1 <= score <= 10:
                raise ValueError("Monitor quality scores must be integers from 1 to 10")
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Monitor quality assessments require a nonempty reason")
            references = item.get("source_ids")
            if (not isinstance(references, list) or not references
                    or any(not isinstance(ref, str) or ref not in source_ids for ref in references)):
                raise ValueError("Monitor quality must cite existing snapshot source IDs")
            assessments[dimension] = {
                "score": score, "reason": reason,
                "source_ids": list(dict.fromkeys(references)),
            }
        result["quality"] = assessments
    return result


class ResearchMonitor:
    """Run one summary at a time without holding a host lock during model calls."""

    def __init__(
        self,
        read_port: DashboardReadPort,
        monitor_port: MonitorPort,
        cache_dir: Path,
        *,
        auto_refresh_seconds: float = 5400,
        auto_start: bool = True,
    ) -> None:
        if auto_refresh_seconds <= 0:
            raise ValueError("Monitor refresh interval must be positive")
        self._read_port = read_port
        self._monitor_port = monitor_port
        self._cache_file = Path(cache_dir) / "monitor.json"
        self._interval = float(auto_refresh_seconds)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._requested = False
        self._automatic_attempted = False
        self._next_due = time.time()
        self._state: dict[str, Any] = {
            "status": "idle",
            "generated_at": None,
            "next_refresh_at": _timestamp(self._next_due),
            "error": None,
            "result": None,
            "source_as_of": None,
            "source_cycle": None,
        }
        self._load_cache()
        if auto_start:
            self.start()

    def _load_cache(self) -> None:
        try:
            cached = json.loads(self._cache_file.read_text(encoding="utf-8"))
            references = cached["source_ids"]
            if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
                raise ValueError("Invalid cached source IDs")
            result = validate_summary(cached["result"], set(references))
            generated_at = cached["generated_at"]
            generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated.tzinfo is None:
                raise ValueError("Cached timestamp must include a timezone")
            self._next_due = min(generated.timestamp(), time.time()) + self._interval
            self._state.update(
                status="ready", result=result, generated_at=generated_at,
                source_as_of=cached.get("source_as_of"),
                source_cycle=cached.get("source_cycle"),
                next_refresh_at=_timestamp(self._next_due),
            )
            self._automatic_attempted = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # The cache is disposable; an incomplete old file never blocks research.
            pass

    def _save_cache(self, payload: Mapping[str, Any]) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=".monitor-", suffix=".tmp", dir=self._cache_file.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, self._cache_file)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def start(self) -> None:
        with self._condition:
            if self._thread is not None or self._stopped:
                return
            self._thread = threading.Thread(target=self._run, name="dashboard-monitor", daemon=True)
            self._thread.start()

    def status(self) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(self._state)

    def refresh(self) -> dict[str, Any]:
        """Coalesce repeated requests, including clicks during a running summary."""

        with self._condition:
            if self._stopped:
                raise RuntimeError("Dashboard monitor is stopped")
            if self._state["status"] != "running":
                self._requested = True
                self._state["status"] = "running"
                self._state["error"] = None
                self._condition.notify_all()
            return copy.deepcopy(self._state)

    def stop(self, *, timeout: float = 1.0) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    close = stop

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopped and not self._requested and time.time() < self._next_due:
                    self._condition.wait(timeout=max(0, self._next_due - time.time()))
                if self._stopped:
                    return
                manual = self._requested
                self._requested = False
                self._state.update(status="running", error=None)
            try:
                snapshot = self._read_port.monitor_snapshot()
                references = snapshot.get("source_ids", [])
                if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
                    raise ValueError("Monitor snapshot source_ids must be a list of strings")
                run = snapshot.get("run")
                run_status = snapshot.get("run_status")
                if run_status is None and isinstance(run, Mapping):
                    run_status = run.get("status")
                active = run_status in {"running", "waiting_for_human"}
                automatic_allowed = active or (run_status is None and not self._automatic_attempted)
                if references and (manual or automatic_allowed):
                    with self._condition:
                        if self._stopped:
                            return
                    if not manual:
                        self._automatic_attempted = True
                    result = validate_summary(
                        self._monitor_port.summarize(snapshot), set(references), require_quality=True,
                    )
                    generated_at = _timestamp(time.time())
                    payload = {
                        "result": result,
                        "generated_at": generated_at,
                        "source_as_of": snapshot.get("as_of"),
                        "source_ids": references,
                        "source_cycle": run.get("cycle") if isinstance(run, Mapping) else None,
                    }
                    with self._condition:
                        if self._stopped:
                            return
                        self._save_cache(payload)
                        self._state.update(
                            status="ready", result=result, generated_at=generated_at,
                            source_as_of=payload["source_as_of"], error=None,
                            source_cycle=payload["source_cycle"],
                        )
                else:
                    with self._condition:
                        self._state.update(status="ready" if self._state["result"] is not None else "idle", error=None)
            except Exception as exc:
                with self._condition:
                    self._state.update(status="error", error=str(exc) or type(exc).__name__)
            with self._condition:
                if self._stopped:
                    return
                self._next_due = time.time() + self._interval
                self._state["next_refresh_at"] = _timestamp(self._next_due)


__all__ = ["ResearchMonitor", "validate_summary"]
