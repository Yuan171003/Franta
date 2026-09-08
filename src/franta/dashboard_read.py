"""Read-only Franta projections for the independent dashboard program.

No runtime, scheduler, or writable repository is constructed here.  SQLite
connections use mode=ro, query_only and a SELECT-only authorizer.  Normal WAL
reader coordination is retained: SQLite may manage its -shm/-wal sidecars, but
these readers cannot modify canonical records, scheduler state or audit logs.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping

from .human_guidance import read_human_guidance_inbox
from .render import render_record
from .store import MemoryStore


_MAIN_TYPES = ("fact", "route", "claim", "obligation")
_VISIBLE_WORKER_KINDS = {"main", "worker", "explorer-worker", "main-sort"}
_LIVE_TASK_STATES = {
    "launching", "running", "attempt_ended", "postprocessing",
    "revision_pending", "retry_pending", "stopping",
}
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE,
}


def _authorize(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


@contextmanager
def _read_connection(path: Path) -> Iterator[sqlite3.Connection | None]:
    if not path.is_file():
        yield None
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorize)
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


class _CanonicalView:
    """Only the canonical store's pure record decoder, without its writer API.

    Explicitly sharing these methods preserves reciprocal links, revocations,
    ROOT resolution and deferred-reference overlays without reimplementing
    canonical semantics.  The connection also rejects any future accidental
    SQL write from one of these methods.
    """

    _get_locked = MemoryStore._get_locked
    _status_locked = MemoryStore._status_locked
    _current_route_ids_for_memory_locked = MemoryStore._current_route_ids_for_memory_locked
    _current_route_targets_locked = MemoryStore._current_route_targets_locked
    _overlay_simple_soft_references_locked = MemoryStore._overlay_simple_soft_references_locked
    _overlay_deferred_relations_locked = MemoryStore._overlay_deferred_relations_locked
    _relation_view_locked = MemoryStore._relation_view_locked
    _soft_reference_display_value = staticmethod(MemoryStore._soft_reference_display_value)
    _set_exact_path = staticmethod(MemoryStore._set_exact_path)
    _dedupe_relation_reference_lists = staticmethod(MemoryStore._dedupe_relation_reference_lists)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _page(offset: int, limit: int) -> None:
    if _number(offset) is None or _number(limit) is None or not 1 <= limit <= 500:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 500")


def _state(connection: sqlite3.Connection | None) -> tuple[int | None, dict[str, Any]]:
    if connection is None:
        return None, {}
    row = connection.execute(
        "SELECT revision,payload_json FROM control_state WHERE state_key='scheduler.v1'"
    ).fetchone()
    return (None, {}) if row is None else (int(row["revision"]), json.loads(row["payload_json"]))


def _relations(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for field, kind in (
        ("related_route_ids", "related_route"),
        ("related_obligation_ids", "related_obligation"),
        ("active_fact_ids", "active_fact"),
        ("relevant_claim_ids", "relevant_claim"),
        ("relevant_memo_ids", "relevant_memo"),
        ("predecessor_fact_ids", "predecessor_fact"),
    ):
        result.extend({"kind": kind, "target_id": target} for target in data.get(field, []))
    for index, relation in enumerate(data.get("relations", [])):
        if not isinstance(relation, Mapping):
            continue
        for field, kind in (
            ("premise_memory_ids", "premise"),
            ("supporting_fact_ids", "supporting_fact"),
            ("conclusion", "conclusion"),
        ):
            targets = relation.get(field, [])
            if isinstance(targets, str):
                targets = [targets]
            result.extend(
                {
                    "kind": kind, "target_id": target, "relation_index": index,
                    "relation_kind": relation.get("relation_type"),
                    "evidence_status": relation.get("evidence_status"),
                }
                for target in targets
            )
    return result


def _main_item(view: _CanonicalView, record_id: str) -> dict[str, Any]:
    record = view._get_locked(record_id)
    data = record.to_dict()
    return {
        "id": record.memory_id, "type": record.memory_type.value,
        "status": record.status, "active": record.active,
        "abstract": data.get("abstract", ""), "content": render_record(record),
        "relations": _relations(data), "created_at": record.created_at,
        "updated_at": record.updated_at, "revision": record.revision,
        "data": data,
    }


def _graph_item(view: _CanonicalView, record_id: str) -> dict[str, Any]:
    record = view._get_locked(record_id)
    data = record.to_dict()
    return {
        "id": record.memory_id, "type": record.memory_type.value,
        "status": record.status, "active": record.active,
        "abstract": data.get("abstract", ""), "relations": _relations(data),
        "created_at": record.created_at, "updated_at": record.updated_at,
    }


def _explorer_item(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for field in ("related_memory_ids", "cas_operation_ids", "directions_tried"):
        data[field] = json.loads(data.pop(field + "_json") or "[]")
    data["source_scratch_ids"] = [
        item["scratch_id"] for item in connection.execute(
            "SELECT scratch_id FROM explorer_summary_sources WHERE summary_id=? ORDER BY ordinal",
            (row["record_id"],),
        )
    ]
    return {
        "id": row["record_id"], "type": row["record_type"],
        "created_at": row["created_at"], "seq": int(row["visible_seq"]),
        "worker_session_id": row["worker_session_id"], "attempt_no": row["attempt_no"],
        "turn_id": row["turn_id"], "abstract": row["abstract"],
        "content": row["content"], "data": data,
    }


_EXPLORER_SELECT = (
    "SELECT r.*,v.seq AS visible_seq FROM explorer_records r "
    "JOIN explorer_record_visibility v ON v.record_id=r.record_id "
)


class _CallLog:
    """Incremental, partial-line-safe accounting of physical transport launches."""

    def __init__(self) -> None:
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.modified_ns: int | None = None
        self.tail = b""
        self.launches: dict[str, dict[str, Any]] = {}
        self.current: dict[str, Any] | None = None

    def update(self, path: Path) -> None:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset or (
                stat.st_size == self.offset and self.modified_ns is not None
                and stat.st_mtime_ns != self.modified_ns
            ):
                self.__init__()
                self.identity = identity
            self.modified_ns = stat.st_mtime_ns
            stream.seek(self.offset)
            while chunk := stream.read(64 * 1024):
                self.offset += len(chunk)
                lines = (self.tail + chunk).split(b"\n")
                self.tail = lines.pop()
                for line in lines:
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(item, dict):
                        self._accept(item)

    def _accept(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "transport.call_started":
            key = json.dumps(
                [item.get("lease_epoch"), item.get("launch_attempt"), item.get("time")],
                sort_keys=True,
            )
            self.current = self.launches.setdefault(key, {
                "started_at": item.get("time"), "ended_at": None,
                "updated_at": item.get("time"), "reports": {},
                "lease_epoch": item.get("lease_epoch"), "launch_attempt": item.get("launch_attempt"),
            })
            return
        if self.current is None:
            # Legacy/direct JSONL still contributes costs, without an invented start time.
            self.current = self.launches.setdefault("legacy", {
                "started_at": None, "ended_at": None, "updated_at": None, "reports": {},
            })
        if item.get("time"):
            self.current["updated_at"] = item["time"]
        if kind == "transport.call_ended":
            self.current["ended_at"] = item.get("time")
            return
        event = item.get("event") if kind == "transport.codex_event" else item
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            return
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return
        report = {name: _number(usage.get(name)) for name in (
            "input_tokens", "cached_input_tokens", "output_tokens",
        )}
        if all(value is None for value in report.values()):
            return
        # The same terminal event may be replayed, but a new physical launch
        # with identical counts incurred real costs and gets its own bucket.
        key = str(event.get("id") or event.get("turn_id") or json.dumps(event, sort_keys=True))
        self.current["reports"][key] = report


class FrantaDashboardRead:
    def __init__(self, project: str | Path) -> None:
        self.root = Path(project).resolve()
        self.database = self.root / "scheduler.sqlite3"
        self.explorer_database = self.root / "private/explorer.sqlite3"
        self._logs: dict[Path, _CallLog] = {}
        self._log_lock = threading.Lock()

    def _project(self, state: Mapping[str, Any]) -> dict[str, Any]:
        config = _json_file(self.root / "private/runtime-config.json")
        return {
            "name": config.get("project_name") or self.root.name,
            "root_problem": state.get("root", {}).get("problem") or config.get("root_problem", ""),
        }

    def main_memory(self, *, kind: str = "all", query: str = "", offset: int = 0,
                    limit: int = 50) -> dict[str, Any]:
        _page(offset, limit)
        if kind != "all" and kind not in _MAIN_TYPES:
            raise ValueError("unknown main memory type")
        with _read_connection(self.database) as connection:
            revision, _ = _state(connection)
            if connection is None:
                return {"items": [], "total": 0, "offset": offset, "limit": limit, "revision": revision}
            kinds = _MAIN_TYPES if kind == "all" else (kind,)
            condition = "memory_type IN (" + ",".join("?" for _ in kinds) + ")"
            parameters: list[Any] = list(kinds)
            if query.strip():
                condition += " AND (instr(lower(memory_id),lower(?)) OR instr(lower(abstract),lower(?)) OR instr(lower(core_json),lower(?)) OR instr(lower(metadata_json),lower(?)))"
                parameters.extend([query.strip()] * 4)
            total = connection.execute("SELECT COUNT(*) FROM memories WHERE " + condition, parameters).fetchone()[0]
            rows = connection.execute(
                "SELECT memory_id FROM memories WHERE " + condition +
                " ORDER BY updated_at DESC,memory_id DESC LIMIT ? OFFSET ?", (*parameters, limit, offset),
            ).fetchall()
            view = _CanonicalView(connection)
            items = [_main_item(view, row["memory_id"]) for row in rows]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": revision}

    def main_record(self, record_id: str) -> dict[str, Any] | None:
        with _read_connection(self.database) as connection:
            if connection is None:
                return None
            row = connection.execute("SELECT memory_type FROM memories WHERE memory_id=?", (record_id,)).fetchone()
            if row is None or row["memory_type"] not in (*_MAIN_TYPES, "memo"):
                return None
            return _main_item(_CanonicalView(connection), record_id)

    def memory_graph(self) -> dict[str, Any]:
        with _read_connection(self.database) as connection:
            revision, _ = _state(connection)
            if connection is None:
                return {"nodes": [], "edges": [], "revision": revision}
            rows = connection.execute(
                "SELECT memory_id FROM memories WHERE memory_type IN (?,?,?,?) ORDER BY memory_id",
                _MAIN_TYPES,
            ).fetchall()
            view = _CanonicalView(connection)
            nodes = [_graph_item(view, row["memory_id"]) for row in rows]
        node_ids = {node["id"] for node in nodes}
        seen: set[tuple[str, str]] = set()
        edges: list[dict[str, Any]] = []
        for node in nodes:
            for relation in node.pop("relations"):
                target = relation.get("target_id")
                if not isinstance(target, str) or target == node["id"] or target not in node_ids:
                    continue
                pair = tuple(sorted((node["id"], target)))
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append({
                    "source": node["id"], "target": target,
                    "kind": relation.get("relation_kind") or relation.get("kind") or "related",
                })
        return {"nodes": nodes, "edges": edges, "revision": revision}

    def explorer_memory(self, *, record_type: str, query: str = "", offset: int = 0,
                        limit: int = 50) -> dict[str, Any]:
        _page(offset, limit)
        if record_type not in {"scratch", "summary"}:
            raise ValueError("Explorer record_type must be scratch or summary")
        with _read_connection(self.explorer_database) as connection:
            if connection is None:
                return {"items": [], "total": 0, "offset": offset, "limit": limit, "revision": 0}
            high_water = connection.execute("SELECT COALESCE(MAX(seq),0) FROM explorer_record_visibility").fetchone()[0]
            condition = "WHERE r.record_type=?"
            parameters: list[Any] = [record_type]
            if query.strip():
                condition += " AND (instr(lower(r.record_id),lower(?)) OR instr(lower(r.abstract),lower(?)) OR instr(lower(r.content),lower(?)))"
                parameters.extend([query.strip()] * 3)
            total = connection.execute(
                "SELECT COUNT(*) FROM explorer_records r JOIN explorer_record_visibility v ON v.record_id=r.record_id " + condition,
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                _EXPLORER_SELECT + condition + " ORDER BY v.seq DESC,r.record_id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            items = [_explorer_item(connection, row) for row in rows]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": high_water}

    def _latest_explorer(self, state: Mapping[str, Any]) -> list[dict[str, Any]]:
        sessions = [
            key for key, lineage in state.get("explorer_control", {}).get("lineages", {}).items()
            if lineage.get("status") in {"ready", "running", "continuation_pending"}
        ]
        if not sessions:
            return []
        with _read_connection(self.explorer_database) as connection:
            if connection is None:
                return []
            rows = connection.execute(
                _EXPLORER_SELECT + "JOIN (SELECT r2.worker_session_id,r2.attempt_no,MAX(v2.seq) AS latest_seq "
                "FROM explorer_records r2 JOIN explorer_record_visibility v2 ON v2.record_id=r2.record_id "
                "WHERE r2.worker_session_id IN (" + ",".join("?" for _ in sessions) + ") "
                "GROUP BY r2.worker_session_id,r2.attempt_no) latest ON latest.latest_seq=v.seq",
                sessions,
            ).fetchall()
            return [_explorer_item(connection, row) for row in rows]

    @staticmethod
    def _directions(state: Mapping[str, Any], explorer: list[dict[str, Any]]) -> list[dict[str, Any]]:
        directions = []
        for task_id, task in state.get("tasks", {}).items():
            if task.get("state") not in _LIVE_TASK_STATES:
                continue
            card = task.get("task_card") or task.get("assign_record") or {}
            attempts = task.get("attempts") or []
            attempt = attempts[-1] if attempts else {}
            directions.append({
                "id": task_id, "system": "franta", "task_id": task_id,
                "session_id": task.get("session_lineage_id"),
                "attempt": task.get("current_attempt"), "status": task.get("state"),
                "objective": card.get("objective") or "Research objective not reported",
                "mode": card.get("mode"),
                "updated_at": attempt.get("ended_at") or attempt.get("started_at"),
            })
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for item in explorer:
            key = (item["worker_session_id"], item["attempt_no"])
            if key not in latest or latest[key]["seq"] < item["seq"]:
                latest[key] = item
        for lineage_id, lineage in state.get("explorer_control", {}).get("lineages", {}).items():
            if lineage.get("status") not in {"ready", "running", "continuation_pending"}:
                continue
            attempts = lineage.get("attempts") or []
            attempt = attempts[-1] if attempts else {}
            number = attempt.get("attempt_number")
            item = latest.get((lineage_id, number))
            reported = item.get("data", {}).get("directions_tried") if item else []
            objective = "; ".join(reported) if reported else (item["abstract"] if item else "Research direction not reported")
            directions.append({
                "id": lineage_id, "system": "explorer", "task_id": None,
                "session_id": lineage_id, "attempt": number,
                "status": lineage.get("status"), "objective": objective,
                "mode": "explorer", "updated_at": item["created_at"] if item else attempt.get("started_at") or lineage.get("admitted_at"),
                "source_ids": [item["id"]] if item else [],
            })
        return sorted(directions, key=lambda item: (item.get("updated_at") or "", item["id"]), reverse=True)

    @staticmethod
    def _workers(state: Mapping[str, Any], *, active: bool) -> list[dict[str, Any]]:
        """Project live call state into the small operator-facing worker roster."""

        if not active:
            return []
        workers: list[dict[str, Any]] = []
        for call_id, call in state.get("calls", {}).items():
            kind = str(call.get("kind") or "")
            if kind not in _VISIBLE_WORKER_KINDS or call.get("status") != "running":
                continue
            launch_input = call.get("input") if isinstance(call.get("input"), Mapping) else {}
            task_card = launch_input.get("task_card") if isinstance(launch_input.get("task_card"), Mapping) else {}
            payload = launch_input.get("payload") if isinstance(launch_input.get("payload"), Mapping) else launch_input
            identifiers = (
                task_card.get("task_id"), payload.get("task_id"), payload.get("worker_session_id"),
                payload.get("lineage_id"), payload.get("sort_run_id"), call_id,
            )
            objective = task_card.get("objective") or payload.get("objective")
            if not objective:
                objective = {
                    "main": "Coordinating the current research cycle",
                    "worker": "Working on an assigned Franta task",
                    "explorer-worker": "Exploring an independent research direction",
                    "main-sort": "Reviewing and promoting Explorer findings",
                }[kind]
            workers.append({
                "call_id": call_id,
                "kind": kind,
                "worker_id": next((str(value) for value in identifiers if value), call_id),
                "mode": task_card.get("mode") or payload.get("access_mode") or call.get("mode"),
                "objective": objective,
                "status": "running",
                "attempt": call.get("attempt"),
            })
        return sorted(workers, key=lambda item: (item["kind"], item["call_id"]))

    def _usage_and_run(self, state: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
        runner = _json_file(self.root / "private/dashboard/runner.json")
        alive = False
        pid = runner.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:
                alive = True
            except ProcessLookupError:
                pass
        status = str(runner.get("status") or "unknown")
        if status == "running" and not alive:
            status = "stopped"
        if state.get("gate") == "completed":
            status = "completed"
        elif state.get("gate") == "waiting_for_human" and status in {"unknown", "running"}:
            status = "waiting_for_human"
        runner_start = _instant(runner.get("started_at"))
        log_dir = self.root / "private/transport/calls"
        if not log_dir.is_dir():
            log_dir = self.root / "private/codex/calls"
        with self._log_lock:
            paths = list(log_dir.glob("*.jsonl")) if log_dir.is_dir() else []
            for path in paths:
                try:
                    self._logs.setdefault(path, _CallLog()).update(path)
                except OSError:
                    continue
            launches = [launch for log in self._logs.values() for launch in log.launches.values()]
            usage: dict[str, Any] = {}
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                values = [report[field] for launch in launches for report in launch["reports"].values() if report[field] is not None]
                usage[field] = sum(values) if values else None
            usage["total_tokens"] = (
                usage["input_tokens"] + usage["output_tokens"]
                if usage["input_tokens"] is not None and usage["output_tokens"] is not None else None
            )
            usage["reported_calls"] = sum(bool(launch["reports"]) for launch in launches)
            indexed_ids = {path.stem for path, log in self._logs.items() if log.launches}
            missing_logs = sum(
                call_id not in indexed_ids and (_number(call.get("attempt")) or 0) > 0
                for call_id, call in state.get("calls", {}).items()
            )
            usage["unreported_calls"] = len(launches) - usage["reported_calls"] + missing_logs
            intervals: list[tuple[datetime, datetime]] = []
            stamps: list[datetime] = []
            ongoing = set()
            for path, log in self._logs.items():
                call = state.get("calls", {}).get(path.stem, {})
                if log.current is None or call.get("status") != "running":
                    continue
                if log.current.get("lease_epoch") != call.get("lease_epoch"):
                    continue
                if log.current.get("launch_attempt") != call.get("attempt"):
                    continue
                ongoing.add(id(log.current))
            for launch in launches:
                start = _instant(launch["started_at"])
                end = _instant(launch["ended_at"])
                if start is not None:
                    stamps.append(start)
                    live = (
                        status == "running" and id(launch) in ongoing
                        and (runner_start is None or start >= runner_start)
                    )
                    endpoint = end or (now if live else _instant(launch["updated_at"])) or start
                    intervals.append((start, max(start, min(endpoint, now))))
        if runner_start is not None:
            stamps.append(runner_start)
        active_seconds = 0.0
        previous_end: datetime | None = None
        for start, end in sorted(intervals):
            active_seconds += max(0.0, (end - max(start, previous_end or start)).total_seconds())
            previous_end = max(end, previous_end or end)
        start = min(stamps) if stamps else None
        endpoints = [end for _, end in intervals]
        for key in ("ended_at", "updated_at"):
            instant = _instant(runner.get(key))
            if instant:
                endpoints.append(instant)
        end = now if status == "running" else max(endpoints, default=now)
        phase = state.get("phase_control") or {}
        run = {
            "status": status, "phase": phase.get("phase"), "cycle": phase.get("cycle"),
            "started_at": _stamp(start) if start else None,
            "elapsed_seconds": max(0.0, (end - start).total_seconds()) if start else None,
            "active_seconds": active_seconds if intervals else None,
        }
        usage["monitor"] = self._monitor_usage()
        return usage, run

    def _monitor_usage(self) -> dict[str, Any]:
        runs = [
            _json_file(path) for path in
            (self.root / "private/dashboard/monitor-runs").glob("*/usage.json")
        ]
        reports = [
            report for run in runs for report in run.get("usage", [])
            if isinstance(report, dict)
        ]
        result: dict[str, Any] = {}
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            values = [_number(report.get(field)) for report in reports]
            known = [value for value in values if value is not None]
            result[field] = sum(known) if known else None
        result["total_tokens"] = (
            result["input_tokens"] + result["output_tokens"]
            if result["input_tokens"] is not None and result["output_tokens"] is not None else None
        )
        result["reported_calls"] = sum(bool(run.get("usage")) for run in runs)
        result["unreported_calls"] = len(runs) - result["reported_calls"]
        return result

    def _overview(self, state: Mapping[str, Any], explorer: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        project = self._project(state)
        usage, run = self._usage_and_run(state, now)
        control = state.get("advisor_control") or {}
        active = control.get("active") or {}
        history = control.get("history") or []
        latest = active or (history[-1] if history else {})
        report = latest.get("selection_report")
        cycle = (state.get("phase_control") or {}).get("cycle", 1)
        assignments = [item.get("problem_assignment") for item in history if (item.get("problem_assignment") or {}).get("target_cycle") == cycle]
        problem = assignments[0] if len(assignments) == 1 else {"problem_text": project["root_problem"], "original": True}
        guidance = {item["guidance_id"]: {**item, "status": "pending"} for item in read_human_guidance_inbox(self.root)}
        guidance.update(state.get("human_guidance_inbox") or {})
        receipts = [
            _json_file(path) for path in
            (self.root / "private/dashboard/receipts").glob("AFB-*.json")
        ]
        return {
            "project": project, "run": run, "usage": usage,
            "directions": self._directions(state, explorer),
            "workers": self._workers(state, active=run["status"] == "running"),
            "advisor": {
                "status": active.get("status") or ("idle" if control else "disabled"),
                "request_id": report.get("feedback_request_id") if isinstance(report, dict) else None,
                "report": report, "problem": problem,
                "feedback_receipts": sorted(receipts, key=lambda item: item.get("processed_at", ""), reverse=True)[:10],
            },
            "guidance": sorted(guidance.values(), key=lambda item: (item.get("received_at", ""), item.get("guidance_id", "")), reverse=True),
            "as_of": _stamp(now),
        }

    def overview(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with _read_connection(self.database) as connection:
            _, state = _state(connection)
        return self._overview(state, self._latest_explorer(state), now)

    def monitor_snapshot(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        main: list[dict[str, Any]] = []
        with _read_connection(self.database) as connection:
            revision, state = _state(connection)
            if connection is not None:
                view = _CanonicalView(connection)
                ids = connection.execute("SELECT memory_id FROM memories WHERE memory_type IN ('fact','route','claim','obligation','memo') ORDER BY memory_id").fetchall()
                main = [_main_item(view, row["memory_id"]) for row in ids]
        explorer: list[dict[str, Any]] = []
        high_water = 0
        with _read_connection(self.explorer_database) as connection:
            if connection is not None:
                high_water = connection.execute("SELECT COALESCE(MAX(seq),0) FROM explorer_record_visibility").fetchone()[0]
                explorer = [_explorer_item(connection, row) for row in connection.execute(_EXPLORER_SELECT + "ORDER BY v.seq DESC,r.record_id DESC").fetchall()]
        overview = self._overview(state, explorer, now)
        return {
            "project": overview["project"], "as_of": overview["as_of"],
            "run": {
                **overview["run"],
                "cycle_started_at": ((state.get("phase_control") or {}).get("explorer") or {}).get("admission_started_at"),
            },
            "problem_assignment": overview["advisor"]["problem"],
            "main": main, "explorer": explorer, "directions": overview["directions"],
            "source_revision": revision, "explorer_high_water_seq": high_water,
            "source_ids": [item["id"] for item in (*main, *explorer)],
        }


__all__ = ["FrantaDashboardRead"]
