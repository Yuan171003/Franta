"""Read-only evaluation of durable Franta runs and live-session observations.

The evaluators in this module report evidence; they never repair state, invoke
an agent, or write to the project being inspected.  Mathematical quality and
research balance remain semantic judgements.  In particular, memory counts are
reported without turning them into quotas or category-split rules.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO
from urllib.parse import quote


MEMORY_TYPES = (
    "fact",
    "route",
    "memo",
    "claim",
    "obligation",
    "task",
    "computation",
)
SEARCH_ACTIONS = frozenset({"internal_search", "search"})
FETCH_ACTIONS = frozenset({"memory_fetch", "fetch", "full_record_read"})
# Observation names pass through ``_normalized_name``, which uses underscores.
ISOLATED_MODES = frozenset({"brainstorm", "multi_discipline"})
GATE_STATES = frozenset(
    {
        "open",
        "reviewing_trim",
        "trimming",
        "waiting_for_human",
        "resolution_pending",
        "completed",
    }
)
TASK_STATES = frozenset(
    {
        "queued",
        "launching",
        "running",
        "attempt_ended",
        "postprocessing",
        "revision_pending",
        "retry_pending",
        "stopping",
        "needs_attention",
        "closed",
    }
)
LIVE_CALL_STATES = frozenset({"prepared", "running", "retry_pending", "completed"})


class ScenarioStatus(str, Enum):
    """Outcome of one evaluation scenario."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: ScenarioStatus
    summary: str
    findings: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "findings": list(self.findings),
            "evidence": _json_safe(self.evidence),
        }


@dataclass(frozen=True)
class EvaluationReport:
    scenarios: tuple[ScenarioResult, ...]
    diagnostics: tuple[str, ...] = ()
    source: str | None = None
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def status(self) -> ScenarioStatus:
        statuses = {item.status for item in self.scenarios}
        if ScenarioStatus.FAIL in statuses:
            return ScenarioStatus.FAIL
        if ScenarioStatus.INCONCLUSIVE in statuses:
            return ScenarioStatus.INCONCLUSIVE
        return ScenarioStatus.PASS

    def scenario(self, name: str) -> ScenarioResult:
        for item in self.scenarios:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "source": self.source,
            "status": self.status.value,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "diagnostics": list(self.diagnostics),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True
        )


@dataclass(frozen=True)
class ObservationEvent:
    """One normalized, immutable observation from a JSON or JSONL record."""

    kind: str
    source: str
    ordinal: int
    data: Mapping[str, Any]
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "ordinal": self.ordinal,
            "timestamp": self.timestamp,
            "data": _json_safe(self.data),
        }


@dataclass(frozen=True)
class ParsedObservations:
    events: tuple[ObservationEvent, ...]
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "diagnostics": list(self.diagnostics),
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_")


def _containers(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield nested mapping layers commonly used by the audited records."""

    if not isinstance(value, Mapping):
        return
    pending: list[Mapping[str, Any]] = [value]
    seen: set[int] = set()
    nested_keys = (
        "payload",
        "item",
        "arguments",
        "artifact",
        "event",
        "details",
        "query",
        "result",
        "task_card",
        "assign_record",
        "access_policy",
    )
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        for key in nested_keys:
            child = current.get(key)
            if isinstance(child, Mapping):
                pending.append(child)


def _field(event: ObservationEvent | Mapping[str, Any], *names: str) -> Any:
    raw = event.data if isinstance(event, ObservationEvent) else event
    for container in _containers(raw):
        for name in names:
            if name in container:
                return container[name]
    return None


def _list_field(event: ObservationEvent | Mapping[str, Any], *names: str) -> list[Any]:
    value = _field(event, *names)
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _event_success(event: ObservationEvent) -> bool | None:
    # Codex emits an ``item.started`` row before the terminal row for the same
    # tool item.  A started row is useful attempt evidence, but it must never
    # authenticate a successful skill invocation merely because its normalized
    # kind names a known tool.
    outer_type = _normalized_name(event.data.get("type"))
    event_type = _normalized_name(_field(event, "event_type"))
    if outer_type in {"item_started", "tool_started"} or event_type in {
        "item_started",
        "tool_started",
    }:
        return None
    allowed = _field(event, "allowed")
    if isinstance(allowed, bool):
        return allowed
    success = _field(event, "success", "ok")
    if isinstance(success, bool):
        return success
    status = _normalized_name(_field(event, "status", "tool_status"))
    if status in {"failed", "failure", "error", "rejected", "denied", "cancelled"}:
        return False
    if status in {
        "created",
        "in_progress",
        "incomplete",
        "pending",
        "queued",
        "running",
        "started",
    }:
        return None
    if status in {"completed", "complete", "success", "succeeded", "committed"}:
        return True
    if _field(event, "error", "error_type") is not None:
        return False
    if event.kind in {
        "skill_call",
        "internal_search",
        "memory_fetch",
        "fact_dependency_closure",
        "task_summary_read",
        "task_artifact_read",
    }:
        return True
    return None


class LiveSessionObservationParser:
    """Parse live JSONL records without mutating or responding to the run.

    Malformed lines become diagnostics.  The parser deliberately has no repair
    callback and performs no scheduler, broker, or filesystem writes.
    """

    def parse_lines(
        self,
        lines: Iterable[str],
        *,
        source: str = "live-session",
    ) -> ParsedObservations:
        events: list[ObservationEvent] = []
        diagnostics: list[str] = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                diagnostics.append(
                    f"{source}:{line_number}: invalid JSON ({exc.msg})"
                )
                continue
            if not isinstance(value, Mapping):
                diagnostics.append(f"{source}:{line_number}: JSON value is not an object")
                continue
            events.append(self.normalize(value, source=source, ordinal=line_number))
        return ParsedObservations(tuple(events), tuple(diagnostics))

    def parse_text(self, text: str, *, source: str = "live-session") -> ParsedObservations:
        return self.parse_lines(text.splitlines(), source=source)

    def parse_path(self, path: str | os.PathLike[str]) -> ParsedObservations:
        source_path = Path(path)
        try:
            with source_path.open("r", encoding="utf-8") as handle:
                parsed = self.parse_lines(handle, source=str(source_path))
        except OSError as exc:
            return ParsedObservations((), (f"{source_path}: cannot read ({exc})",))
        if source_path.name != "skill_activity.jsonl":
            return parsed

        # A skill audit is stored at <workspace>/outbox/skill_activity.jsonl.
        # Enriching it from the immutable staged artifact makes final-progress
        # and assignment correlation observable without changing either file.
        workspace = source_path.parent.parent.resolve()
        events: list[ObservationEvent] = []
        diagnostics = list(parsed.diagnostics)
        for event in parsed.events:
            data = copy.deepcopy(dict(event.data))
            data.setdefault("call_id", workspace.name)
            relative = data.get("relative_path")
            if isinstance(relative, str):
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    diagnostics.append(
                        f"{source_path}:{event.ordinal}: unsafe staged skill artifact path"
                    )
                else:
                    artifact_path = (workspace / relative_path).resolve()
                    if (
                        workspace in artifact_path.parents
                        and artifact_path.is_file()
                        and not artifact_path.is_symlink()
                    ):
                        try:
                            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            diagnostics.append(
                                f"{artifact_path}: cannot read staged skill artifact ({exc})"
                            )
                        else:
                            if isinstance(artifact, Mapping):
                                data["artifact"] = dict(artifact)
                            else:
                                diagnostics.append(
                                    f"{artifact_path}: staged skill artifact is not an object"
                                )
            events.append(
                ObservationEvent(
                    kind=event.kind,
                    source=event.source,
                    ordinal=event.ordinal,
                    data=data,
                    timestamp=event.timestamp,
                )
            )
        return ParsedObservations(tuple(events), tuple(diagnostics))

    def parse_objects(
        self,
        values: Iterable[Mapping[str, Any] | ObservationEvent],
        *,
        source: str,
    ) -> ParsedObservations:
        events: list[ObservationEvent] = []
        diagnostics: list[str] = []
        for ordinal, value in enumerate(values, 1):
            if isinstance(value, ObservationEvent):
                events.append(value)
            elif isinstance(value, Mapping):
                events.append(self.normalize(value, source=source, ordinal=ordinal))
            else:
                diagnostics.append(
                    f"{source}:{ordinal}: observation is not an object"
                )
        return ParsedObservations(tuple(events), tuple(diagnostics))

    def normalize(
        self,
        value: Mapping[str, Any],
        *,
        source: str,
        ordinal: int,
    ) -> ObservationEvent:
        raw = copy.deepcopy(dict(value))
        timestamp = raw.get("time") or raw.get("created_at") or raw.get("timestamp")
        inner = raw
        if raw.get("type") == "transport.codex_event" and isinstance(
            raw.get("event"), Mapping
        ):
            inner = copy.deepcopy(dict(raw["event"]))
            for key in ("call_id", "role", "mode"):
                if key in raw and key not in inner:
                    inner[key] = raw[key]
            raw["normalized_event"] = inner
        item = inner.get("item") if isinstance(inner.get("item"), Mapping) else None

        if "skill" in inner:
            kind = "skill_call"
        elif item is not None and _normalized_name(item.get("type")) in {
            "mcp_tool_call",
            "tool_call",
            "web_search",
            "web_search_call",
            "command_execution",
        }:
            kind = _normalized_name(
                item.get("tool") or item.get("name") or item.get("type")
            )
        elif inner.get("type") == "tool_activity":
            kind = _normalized_name(
                inner.get("tool") or inner.get("name") or inner.get("tool_type")
            )
        elif "action" in inner:
            kind = _normalized_name(inner.get("action"))
        else:
            kind = _normalized_name(inner.get("type") or inner.get("event_type"))

        if kind == "search":
            kind = "internal_search"
        elif kind == "fetch":
            kind = "memory_fetch"
        elif kind in {"task_summary", "read_task_summary"}:
            kind = "task_summary_read"
        elif kind in {
            "task_artifact",
            "task_artifact_fetch",
            "read_task_artifact",
        }:
            kind = "task_artifact_read"
        return ObservationEvent(
            kind=kind or "unknown",
            source=source,
            ordinal=ordinal,
            data=raw,
            timestamp=str(timestamp) if timestamp is not None else None,
        )


def parse_live_session(
    source: str | os.PathLike[str] | Iterable[str],
    *,
    source_name: str = "live-session",
) -> ParsedObservations:
    """Convenience parser for a JSONL path, JSONL text, or iterable of lines."""

    parser = LiveSessionObservationParser()
    if isinstance(source, os.PathLike):
        return parser.parse_path(source)
    if isinstance(source, str):
        candidate = Path(source)
        if "\n" not in source and candidate.is_file():
            return parser.parse_path(candidate)
        return parser.parse_text(source, source=source_name)
    return parser.parse_lines(source, source=source_name)


def _scope_complete(snapshot: Mapping[str, Any], area: str) -> bool:
    scope = snapshot.get("observation_scope")
    if scope is True:
        return True
    if not isinstance(scope, Mapping):
        return False
    if scope.get("complete") is True:
        return True
    value = scope.get(area)
    if isinstance(value, Mapping):
        return value.get("complete") is True
    return value is True


class _EvaluationContext:
    def __init__(
        self,
        snapshot: Mapping[str, Any],
        events: Sequence[ObservationEvent],
    ) -> None:
        self.snapshot = copy.deepcopy(dict(snapshot))
        self.events = tuple(events)
        scheduler = self.snapshot.get("scheduler")
        self.scheduler: Mapping[str, Any] = (
            scheduler if isinstance(scheduler, Mapping) else {}
        )
        self.tasks: Mapping[str, Any] = (
            self.scheduler.get("tasks", {})
            if isinstance(self.scheduler.get("tasks", {}), Mapping)
            else {}
        )
        self.calls: Mapping[str, Any] = (
            self.scheduler.get("calls", {})
            if isinstance(self.scheduler.get("calls", {}), Mapping)
            else {}
        )

    def event_identity(self, event: ObservationEvent) -> tuple[str, str, str, str]:
        call_id = str(_field(event, "call_id") or "")
        task_id = str(_field(event, "task_id") or "")
        role = _normalized_name(_field(event, "role"))
        mode = _normalized_name(_field(event, "mode", "work_mode"))
        call = self.calls.get(call_id) if call_id else None
        if isinstance(call, Mapping):
            role = role or _normalized_name(call.get("kind"))
            continuation = call.get("continuation")
            if not task_id and isinstance(continuation, Mapping):
                task_id = str(continuation.get("task_id") or "")
        task = self.tasks.get(task_id) if task_id else None
        if isinstance(task, Mapping):
            card = task.get("task_card")
            if isinstance(card, Mapping):
                mode = mode or _normalized_name(card.get("mode", card.get("work_mode")))
            if role in {"", "worker"}:
                role = "worker"
        policy = str(_field(event, "policy") or "")
        normalized_policy = _normalized_name(policy)
        if not role:
            if normalized_policy in {"main", "main_agent"}:
                role = "main"
            elif normalized_policy == "trimmer":
                role = "trimmer"
            elif normalized_policy.startswith("worker_"):
                role = "worker"
        if not mode and policy:
            for candidate in (
                "multi_discipline",
                "proof_writer",
                "brainstorm",
                "reformulate",
                "computation",
                "associate",
                "research",
                "verifier",
            ):
                if candidate.replace("_", "-") in policy or candidate in policy:
                    mode = candidate
                    break
        return call_id, task_id, role, mode


def _result(
    name: str,
    status: ScenarioStatus,
    summary: str,
    findings: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        status=status,
        summary=summary,
        findings=tuple(findings),
        evidence=dict(evidence or {}),
    )


def _evaluate_scheduler_state(context: _EvaluationContext) -> ScenarioResult:
    state = context.scheduler
    if not state:
        return _result(
            "scheduler_state",
            ScenarioStatus.INCONCLUSIVE,
            "No scheduler snapshot was supplied.",
        )
    issues: list[str] = []
    gate = str(state.get("gate") or "")
    if gate not in GATE_STATES:
        issues.append(f"unknown assignment gate {gate!r}")
    tasks = state.get("tasks", {})
    calls = state.get("calls", {})
    events = state.get("events", [])
    if not isinstance(tasks, Mapping):
        issues.append("scheduler.tasks is not an object")
        tasks = {}
    if not isinstance(calls, Mapping):
        issues.append("scheduler.calls is not an object")
        calls = {}
    if not isinstance(events, list):
        issues.append("scheduler.events is not a list")
        events = []

    event_ids = [item.get("event_id") for item in events if isinstance(item, Mapping)]
    if event_ids and event_ids != list(range(1, len(event_ids) + 1)):
        issues.append("scheduler event IDs are not a contiguous append-only sequence")
    for task_id, raw in tasks.items():
        if not isinstance(raw, Mapping):
            issues.append(f"task {task_id} is not an object")
            continue
        if raw.get("state") not in TASK_STATES:
            issues.append(f"task {task_id} has unknown state {raw.get('state')!r}")
        attempts = raw.get("attempts", [])
        if isinstance(attempts, list):
            numbers = [item.get("attempt") for item in attempts if isinstance(item, Mapping)]
            if numbers and numbers != list(range(1, len(numbers) + 1)):
                issues.append(f"task {task_id} attempt numbers are not contiguous")

    for kind in ("main", "trimmer"):
        active = [
            call_id
            for call_id, call in calls.items()
            if isinstance(call, Mapping)
            and call.get("kind") == kind
            and call.get("status") in LIVE_CALL_STATES
        ]
        if len(active) > 1:
            issues.append(f"more than one live {kind} call exists: {active}")
    limits = state.get("limits", {}) if isinstance(state.get("limits"), Mapping) else {}
    max_workers = limits.get("max_non_verifier_workers")
    reserved = sum(
        1
        for task in tasks.values()
        if isinstance(task, Mapping)
        and task.get("slot_reserved")
        and task.get("state") != "closed"
    )
    if isinstance(max_workers, int) and reserved > max_workers:
        issues.append(
            f"reserved non-verifier slots ({reserved}) exceed the configured limit ({max_workers})"
        )
    max_verifiers = limits.get("max_parallel_verifiers")
    live_verifiers = sum(
        1
        for call in calls.values()
        if isinstance(call, Mapping)
        and call.get("kind") == "verifier"
        and call.get("status") in LIVE_CALL_STATES
    )
    if isinstance(max_verifiers, int) and live_verifiers > max_verifiers:
        issues.append(
            f"live verifier calls ({live_verifiers}) exceed the configured limit ({max_verifiers})"
        )

    if issues:
        return _result(
            "scheduler_state",
            ScenarioStatus.FAIL,
            "The durable scheduler snapshot violates mechanical state invariants.",
            issues,
            {"gate": gate, "tasks": len(tasks), "calls": len(calls)},
        )
    return _result(
        "scheduler_state",
        ScenarioStatus.PASS,
        "The supplied scheduler snapshot is mechanically coherent.",
        evidence={
            "gate": gate,
            "tasks": len(tasks),
            "calls": len(calls),
            "events": len(events),
            "reserved_non_verifier_slots": reserved,
            "live_verifiers": live_verifiers,
        },
    )


def _record_type(record: Mapping[str, Any]) -> str:
    return _normalized_name(record.get("type", record.get("memory_type")))


def _evaluate_memory_health(context: _EvaluationContext) -> ScenarioResult:
    raw_records = context.snapshot.get("memories", [])
    if not isinstance(raw_records, Sequence) or isinstance(
        raw_records, (str, bytes, bytearray)
    ):
        return _result(
            "memory_health",
            ScenarioStatus.FAIL,
            "The memory observation is malformed.",
            ("snapshot.memories must be a list",),
        )
    records = [item for item in raw_records if isinstance(item, Mapping)]
    counts = Counter(_record_type(item) for item in records)
    count_report = {kind: counts.get(kind, 0) for kind in MEMORY_TYPES}
    active_counts = Counter(
        _record_type(item) for item in records if bool(item.get("active", True))
    )
    inactive_counts = Counter(
        _record_type(item) for item in records if not bool(item.get("active", True))
    )
    issues: list[str] = []
    qualitative_signals = Counter()
    for index, record in enumerate(records):
        memory_id = str(record.get("id", record.get("memory_id", f"record[{index}]")))
        kind = _record_type(record)
        if kind not in MEMORY_TYPES:
            issues.append(f"{memory_id} has unknown memory type {kind!r}")
            continue
        if kind in {"fact", "route", "memo", "claim", "obligation"} and not str(
            record.get("abstract") or ""
        ).strip():
            issues.append(f"{memory_id} lacks a searchable abstract")
        if kind == "fact" and record.get("active", True):
            required = (
                "statement",
                "proof",
                "originating_task_id",
                "foundation_policy_version",
            )
            missing = [key for key in required if not record.get(key)]
            if missing:
                issues.append(f"active fact {memory_id} lacks {missing}")
            else:
                qualitative_signals["fact_complete_proof"] += 1
        elif kind == "route":
            assessment = record.get("value_assessment")
            expected = {
                "confidence",
                "success_gain",
                "failure_gain",
                "relevance",
                "novelty",
            }
            if not isinstance(assessment, Mapping) or not expected <= set(assessment):
                issues.append(f"route {memory_id} lacks its qualitative value assessment")
            elif any(isinstance(assessment[key], (int, float, bool)) for key in expected):
                issues.append(f"route {memory_id} uses numeric rather than qualitative value signals")
            else:
                qualitative_signals["route_value_assessment"] += 1
        elif kind == "obligation":
            if not str(record.get("statement") or "").strip() or not str(
                record.get("importance") or ""
            ).strip():
                issues.append(f"obligation {memory_id} lacks statement or qualitative importance")
            else:
                qualitative_signals["obligation_importance"] += 1

    reviews: list[dict[str, Any]] = []
    for event in context.events:
        if event.kind not in {"memory_health_review", "memory_importance_review"}:
            continue
        verdict = _normalized_name(_field(event, "verdict", "status"))
        rationale = str(_field(event, "rationale", "reason", "summary") or "")
        reviewed_types = sorted(
            {
                _normalized_name(item)
                for item in _list_field(
                    event,
                    "memory_types",
                    "reviewed_memory_types",
                    "types",
                )
                if _normalized_name(item) in MEMORY_TYPES
            }
        )
        reviews.append(
            {
                "kind": event.kind,
                "verdict": verdict,
                "rationale": rationale,
                "reviewed_memory_types": reviewed_types,
            }
        )
        if verdict in {"unhealthy", "fail", "failed"}:
            issues.append("a qualitative memory review judged the memory set unhealthy")

    evidence = {
        "counts": count_report,
        "active_counts": {kind: active_counts.get(kind, 0) for kind in MEMORY_TYPES},
        "inactive_counts": {
            kind: inactive_counts.get(kind, 0) for kind in MEMORY_TYPES
        },
        "core_memory_counts": {
            kind: count_report[kind] for kind in ("fact", "route", "obligation")
        },
        "exploratory_memory_counts": {
            kind: count_report[kind] for kind in ("memo", "claim")
        },
        "qualitative_schema_signals": dict(qualitative_signals),
        "qualitative_reviews": reviews,
        "count_policy": "counts are observations only; no numerical health quota is applied",
    }
    if issues:
        return _result(
            "memory_health",
            ScenarioStatus.FAIL,
            "Structural or explicit qualitative evidence identifies unhealthy memory records.",
            issues,
            evidence,
        )
    if any(
        review["verdict"] in {"healthy", "pass", "passed"}
        for review in reviews
    ):
        return _result(
            "memory_health",
            ScenarioStatus.PASS,
            "Required qualitative record signals are present and a semantic review found the mix healthy.",
            evidence=evidence,
        )
    return _result(
        "memory_health",
        ScenarioStatus.INCONCLUSIVE,
        "Counts and schema signals were recorded, but mathematical importance cannot be judged mechanically.",
        (
            "No numerical quota, fact/claim ratio, or forced category split was inferred from the counts.",
        ),
        evidence,
    )


def _skill_name(event: ObservationEvent) -> str:
    if event.kind == "skill_call":
        return str(_field(event, "skill") or "")
    if event.kind in {
        "internal_search",
        "task_summary_read",
        "task_artifact_read",
        "task_writing",
        "record_progress",
        "cas",
        "human_guidance",
        "discovery_sprint",
    }:
        if event.kind in {"task_summary_read", "task_artifact_read"}:
            return "task-search"
        return event.kind.replace("_", "-")
    return ""


def _skill_allowed(skill: str, role: str, mode: str, event: ObservationEvent) -> bool | None:
    skill = skill.casefold()
    role = role.replace("_", "-")
    mode = mode.replace("_", "-")
    if skill == "task-search":
        return role in {"main", "main-agent", "trimmer"}
    if role in {"main", "main-agent"}:
        return skill in {"task-writing", "internal-search", "task-search"}
    if role == "trimmer":
        return skill in {
            "internal-search",
            "task-search",
            "human-guidance",
            "discovery-sprint",
        }
    if role == "synthesizer":
        return skill == "internal-search"
    if role in {"summarizer", "discovery-sprint-summarizer"}:
        return False
    if role == "scheduler":
        return skill == "task-writing"
    if role == "verifier" or mode == "verifier":
        return skill in {"internal-search", "cas"}
    if role in {"proof-writer", "proofwriter"} or mode == "proof-writer":
        return skill in {"internal-search", "record-progress", "cas"}
    if role == "worker":
        if skill in {"record-progress", "cas"}:
            return True
        if skill == "internal-search":
            project_api = _field(event, "project_memory_api")
            if isinstance(project_api, bool):
                return project_api
            return mode.replace("-", "_") not in ISOLATED_MODES
        return False
    return None


def _evaluate_skill_use(context: _EvaluationContext) -> ScenarioResult:
    calls = [event for event in context.events if _skill_name(event)]
    issues: list[str] = []
    observed: list[dict[str, Any]] = []
    successes: list[ObservationEvent] = []
    for event in calls:
        skill = _skill_name(event)
        call_id, task_id, role, mode = context.event_identity(event)
        success = _event_success(event)
        allowed = _skill_allowed(skill, role, mode, event)
        observed.append(
            {
                "skill": skill,
                "call_id": call_id or None,
                "task_id": task_id or None,
                "role": role or None,
                "mode": mode or None,
                "success": success,
            }
        )
        if allowed is False:
            issues.append(
                f"{skill} was invoked by disallowed context {role or '?'} / {mode or '?'}"
            )
        if success is True:
            successes.append(event)
        if skill == "task-writing" and _field(event, "batch_finalized") is False:
            issues.append("task-writing ran before the whole batch was finalized")

    complete = _scope_complete(context.snapshot, "skills")
    missing: list[str] = []
    if complete:
        task_writes = [event for event in successes if _skill_name(event) == "task-writing"]
        progress_calls = [event for event in successes if _skill_name(event) == "record-progress"]
        for task_id, task in context.tasks.items():
            if not isinstance(task, Mapping):
                continue
            report = task.get("assign_record")
            report_id = report.get("report_id") if isinstance(report, Mapping) else None
            matched_write = any(
                str(_field(event, "task_id") or "") == str(task_id)
                or (
                    report_id
                    and str(_field(event, "report_id", "operation_id") or "")
                    == str(report_id)
                )
                for event in task_writes
            )
            if not matched_write:
                missing.append(f"task-writing evidence for assignment {task_id}")
            attempts = task.get("attempts", [])
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                number = attempt.get("attempt")
                final_progress_id = attempt.get("final_progress_id")
                if (
                    attempt.get("state") in {"ended", "ended_for_verifier_revision"}
                    and not final_progress_id
                ):
                    missing.append(
                        f"final record-progress evidence for {task_id} attempt {number}"
                    )
                    continue
                if not final_progress_id:
                    # Running and mechanically interrupted attempts do not
                    # create a mandatory final skill call.
                    continue
                matched_progress = any(
                    str(_field(event, "task_id") or "") == str(task_id)
                    and (
                        _field(event, "attempt") in {None, number}
                        or str(_field(event, "attempt")) == str(number)
                    )
                    and _field(event, "final", "is_final") is not False
                    and (
                        _field(event, "progress_id") in {None, final_progress_id}
                        or str(_field(event, "progress_id"))
                        == str(final_progress_id)
                    )
                    for event in progress_calls
                )
                if not matched_progress:
                    missing.append(
                        f"final record-progress evidence for {task_id} attempt {number}"
                    )
        if missing:
            issues.extend(f"missing {item}" for item in missing)

    evidence = {
        "observation_scope_complete": complete,
        "observed_calls": observed,
        "missing_required_evidence": missing,
        "mandatory_usage_policy": (
            "task-writing for assignments and final record-progress for normal worker exits only"
        ),
    }
    if issues:
        return _result(
            "skill_use",
            ScenarioStatus.FAIL,
            "Skill activity contains a wrong-context call or lacks required calls in a complete observation window.",
            issues,
            evidence,
        )
    if not calls:
        return _result(
            "skill_use",
            ScenarioStatus.INCONCLUSIVE,
            "No skill or Codex tool activity was available to inspect.",
            evidence=evidence,
        )
    if not complete:
        return _result(
            "skill_use",
            ScenarioStatus.INCONCLUSIVE,
            "Observed skill calls were context-appropriate, but the log window is not declared complete.",
            evidence=evidence,
        )
    return _result(
        "skill_use",
        ScenarioStatus.PASS,
        "Required calls were observed and every attributable skill call used an allowed context.",
        evidence=evidence,
    )


def _actor_key(context: _EvaluationContext, event: ObservationEvent) -> str:
    call_id, task_id, role, _ = context.event_identity(event)
    source_path = Path(event.source)
    source_call_id = (
        source_path.stem
        if source_path.parent.name == "calls" and source_path.suffix == ".jsonl"
        else ""
    )
    return str(
        _field(event, "caller_id", "actor")
        or call_id
        or task_id
        or role
        or source_call_id
        or event.source
    )


def _mcp_item(event: ObservationEvent) -> Mapping[str, Any] | None:
    raw = event.data
    candidates = [raw]
    for key in ("event", "normalized_event"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    for candidate in candidates:
        item = candidate.get("item")
        if isinstance(item, Mapping):
            return item
    return None


def _read_item_id(event: ObservationEvent) -> str:
    value = _field(event, "item_id")
    if value:
        return str(value)
    item = _mcp_item(event)
    return str(item.get("id") or "") if item is not None else ""


def _search_result_ids(event: ObservationEvent) -> list[str]:
    direct = [str(value) for value in _list_field(event, "result_ids", "memory_ids")]
    if direct:
        return direct
    item = _mcp_item(event)
    result = item.get("result") if item is not None else None
    if not isinstance(result, Mapping):
        return []
    structured = result.get("structured_content", result.get("structuredContent"))
    if not isinstance(structured, Mapping):
        return []
    values = structured.get("result")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [
        str(value["id"])
        for value in values
        if isinstance(value, Mapping) and value.get("id")
    ]


def _authoritative_read_event(event: ObservationEvent) -> bool:
    if event.source == "read_audit" or Path(event.source).name == "agent-reads.jsonl":
        return True
    action = _normalized_name(event.data.get("action"))
    return (
        action
        in {
            "internal_search",
            "memory_fetch",
            "fact_dependency_closure",
            "task_summary_read",
            "task_artifact_read",
        }
        and _field(event, "caller_id") is not None
        and _field(event, "policy") is not None
    )


def _evaluate_progressive_disclosure(context: _EvaluationContext) -> ScenarioResult:
    seen_abstracts: dict[str, set[str]] = defaultdict(set)
    seen_task_summaries: dict[str, set[str]] = defaultdict(set)
    full_reads = 0
    ordered_reads = 0
    unexplained: list[str] = []
    issues: list[str] = []
    relevant = 0
    read_kinds = SEARCH_ACTIONS | FETCH_ACTIONS | {
        "task_summary_read",
        "task_artifact_read",
    }
    authoritative_actors = {
        _actor_key(context, event)
        for event in context.events
        if event.kind in read_kinds
        and _authoritative_read_event(event)
        and _event_success(event) is True
    }
    seen_transport_items: set[tuple[str, str, str]] = set()
    for event in context.events:
        actor = _actor_key(context, event)
        kind = event.kind
        if kind == "progressive_disclosure_violation":
            issues.append(str(_field(event, "reason", "summary") or "explicit violation"))
            continue
        # Transport logs contain a started row and a terminal row for one tool
        # item.  Only a successful terminal row proves that an abstract or full
        # record was actually returned; counting attempts would duplicate reads
        # and could invert their apparent ordering.
        if kind in read_kinds:
            if _event_success(event) is not True:
                continue
            if actor in authoritative_actors and not _authoritative_read_event(event):
                continue
            if not _authoritative_read_event(event):
                item_id = _read_item_id(event)
                if item_id:
                    key = (actor, kind, item_id)
                    if key in seen_transport_items:
                        continue
                    seen_transport_items.add(key)
        if kind in SEARCH_ACTIONS or kind in {
            "abstract_read",
            "portfolio_index_read",
            "category_summary_read",
        }:
            relevant += 1
            for memory_id in _search_result_ids(event):
                seen_abstracts[actor].add(str(memory_id))
            memory_id = _field(event, "memory_id", "subject_id")
            if kind == "abstract_read" and memory_id:
                seen_abstracts[actor].add(str(memory_id))
            continue
        if kind == "task_summary_read":
            relevant += 1
            task_id = _field(event, "task_id", "subject_id")
            if task_id:
                seen_task_summaries[actor].add(str(task_id))
            continue
        if kind in FETCH_ACTIONS:
            relevant += 1
            full_reads += 1
            memory_id = str(_field(event, "memory_id", "subject_id") or "")
            needed = _field(event, "needed")
            justification = _field(event, "justification", "reason")
            if needed is False:
                issues.append(f"{actor} read full record {memory_id or '?'} despite marking it unnecessary")
            elif memory_id and memory_id in seen_abstracts[actor]:
                ordered_reads += 1
            elif justification or needed is True:
                ordered_reads += 1
            else:
                unexplained.append(f"{actor}:{memory_id or '?'}")
            continue
        if kind == "task_artifact_read":
            relevant += 1
            full_reads += 1
            task_id = str(_field(event, "task_id", "subject_id") or "")
            needed = _field(event, "needed")
            justification = _field(event, "justification", "reason")
            if needed is False:
                issues.append(f"{actor} read task artifact {task_id or '?'} despite marking it unnecessary")
            elif task_id and task_id in seen_task_summaries[actor]:
                ordered_reads += 1
            elif justification or needed is True:
                ordered_reads += 1
            else:
                unexplained.append(f"{actor}:task:{task_id or '?'}")

    evidence = {
        "relevant_events": relevant,
        "full_reads": full_reads,
        "reads_with_prior_summary_or_justification": ordered_reads,
        "unexplained_direct_reads": unexplained,
        "policy": "progressive disclosure is evaluated as guidance, not a hard full-read prohibition",
    }
    if issues:
        return _result(
            "progressive_disclosure",
            ScenarioStatus.FAIL,
            "The observations explicitly show unnecessary or declared out-of-order full reads.",
            issues,
            evidence,
        )
    if unexplained:
        return _result(
            "progressive_disclosure",
            ScenarioStatus.INCONCLUSIVE,
            "Some full reads lack a visible earlier abstract or task summary in this observation window.",
            (
                "A missing precursor is not treated as a failure because direct full reads may be necessary and the window may be partial.",
            ),
            evidence,
        )
    if relevant:
        return _result(
            "progressive_disclosure",
            ScenarioStatus.PASS,
            "Observed full reads followed a summary/abstract layer or carried an explicit need justification.",
            evidence=evidence,
        )
    return _result(
        "progressive_disclosure",
        ScenarioStatus.INCONCLUSIVE,
        "No search, summary, abstract, or full-read observations were supplied.",
        evidence=evidence,
    )


def _task_policy(task: Mapping[str, Any]) -> Mapping[str, Any]:
    card = task.get("task_card")
    if isinstance(card, Mapping) and isinstance(card.get("access_policy"), Mapping):
        return card["access_policy"]
    report = task.get("assign_record")
    if isinstance(report, Mapping) and isinstance(report.get("access_policy"), Mapping):
        return report["access_policy"]
    return {}


def _task_mode(task: Mapping[str, Any]) -> str:
    card = task.get("task_card")
    if isinstance(card, Mapping):
        value = card.get("mode", card.get("work_mode"))
        if value:
            return _normalized_name(value)
    report = task.get("assign_record")
    if isinstance(report, Mapping):
        return _normalized_name(report.get("mode", report.get("work_mode")))
    return ""


def _evaluate_isolated_access(context: _EvaluationContext) -> ScenarioResult:
    isolated_tasks: set[str] = set()
    isolated_task_modes: dict[str, str] = {}
    sprint_tasks: set[str] = set()
    sprint_task_modes: dict[str, list[str]] = defaultdict(list)
    sealed_policy_tasks: set[str] = set()
    sprint_summarizer_calls: set[str] = set()
    issues: list[str] = []
    denied_attempts: list[str] = []
    successful_attempts: list[str] = []
    incomplete_attempts: list[str] = []
    for task_id, task in context.tasks.items():
        if not isinstance(task, Mapping):
            continue
        mode = _task_mode(task)
        policy = _task_policy(task)
        sprint_id = str(task.get("sprint_id") or "")
        sprint = bool(sprint_id)
        sealed = policy.get("sealed_workspace") is True or (
            policy.get("project_memory_api") is False
            and policy.get("direct_canonical_mount") is False
        )
        should_isolate = mode in ISOLATED_MODES or sprint or sealed
        if not should_isolate:
            continue
        task_id = str(task_id)
        isolated_tasks.add(task_id)
        isolated_task_modes[task_id] = mode
        if sprint:
            sprint_tasks.add(task_id)
            sprint_task_modes[sprint_id].append(mode)
        project_access = policy.get("canonical_memory", policy.get("project_memory_api"))
        search_access = policy.get("internal_search")
        direct_mount = policy.get("direct_canonical_mount")
        dependency_closure = policy.get("dependency_closure_only")
        allowed_types = policy.get("allowed_memory_types")
        full_record_fetch = policy.get("full_record_fetch")
        if (
            project_access is True
            or search_access is True
            or direct_mount is True
            or dependency_closure is True
            or full_record_fetch is True
            or bool(allowed_types)
        ):
            issues.append(f"isolated task {task_id} has a project-memory capability")
        if not policy:
            issues.append(f"isolated task {task_id} lacks a persisted access policy")
            continue
        if policy.get("sealed_workspace") is not True:
            issues.append(f"isolated task {task_id} is not marked as a sealed workspace")
        elif project_access is not False:
            issues.append(
                f"isolated task {task_id} lacks an explicit project-memory denial"
            )
        elif search_access is not False:
            issues.append(
                f"isolated task {task_id} lacks an explicit internal-search denial"
            )
        else:
            sealed_policy_tasks.add(task_id)

    expected_sprint_modes = Counter(
        ("brainstorm", "multi_discipline", "computation", "associate")
    )
    complete_sprint_ids: set[str] = set()
    for sprint_id, modes in sprint_task_modes.items():
        if Counter(modes) == expected_sprint_modes:
            complete_sprint_ids.add(sprint_id)
        else:
            issues.append(
                f"discovery sprint {sprint_id} does not contain exactly one sealed lane in each required mode"
            )

    sprints = context.scheduler.get("sprints", {})
    if not isinstance(sprints, Mapping):
        sprints = {}
    for call_id, call in context.calls.items():
        if not isinstance(call, Mapping) or _normalized_name(call.get("kind")) not in {
            "summarizer",
            "discovery_sprint_summarizer",
        }:
            continue
        continuation = call.get("continuation")
        sprint_id = (
            str(continuation.get("sprint_id") or "")
            if isinstance(continuation, Mapping)
            else ""
        )
        if not sprint_id:
            continue
        call_id = str(call_id)
        sprint_summarizer_calls.add(call_id)
        sprint = sprints.get(sprint_id)
        frozen = (
            sprint.get("frozen_synthesis_input")
            if isinstance(sprint, Mapping)
            else None
        )
        if not isinstance(frozen, Mapping):
            issues.append(
                f"discovery-sprint summarizer {call_id} lacks a persisted frozen synthesis input"
            )
        elif call.get("input") != frozen:
            issues.append(
                f"discovery-sprint summarizer {call_id} received input beyond or different from the sealed frozen synthesis"
            )

    for event in context.events:
        if event.kind not in {
            "internal_search",
            "memory_fetch",
            "fact_dependency_closure",
            "direct_canonical_access",
        }:
            continue
        _, task_id, role, mode = context.event_identity(event)
        policy_name = str(_field(event, "policy") or "")
        isolated = (
            task_id in isolated_tasks
            or mode in ISOLATED_MODES
            or role in {"summarizer", "discovery_sprint_summarizer"}
            or _field(event, "sprint_lane") is not None
            or _field(event, "sprint_id") is not None
            or "brainstorm" in policy_name
            or "multi-discipline" in policy_name
            or "multi_discipline" in policy_name
        )
        if not isolated:
            continue
        label = f"{task_id or role or policy_name or '?'}:{event.kind}"
        success = _event_success(event)
        if success is False:
            denied_attempts.append(label)
        elif success is True:
            successful_attempts.append(label)
            issues.append(f"isolated context successfully used {event.kind}: {label}")
        else:
            incomplete_attempts.append(label)

    evidence = {
        "isolated_task_ids": sorted(isolated_tasks),
        "isolated_task_modes": dict(sorted(isolated_task_modes.items())),
        "sealed_policy_task_ids": sorted(sealed_policy_tasks),
        "discovery_sprint_task_ids": sorted(sprint_tasks),
        "discovery_sprint_task_modes": {
            sprint_id: sorted(modes)
            for sprint_id, modes in sorted(sprint_task_modes.items())
        },
        "complete_discovery_sprint_ids": sorted(complete_sprint_ids),
        "discovery_sprint_summarizer_call_ids": sorted(sprint_summarizer_calls),
        "coverage": {
            "brainstorm": any(mode == "brainstorm" for mode in isolated_task_modes.values()),
            "multi_discipline": any(
                mode == "multi_discipline" for mode in isolated_task_modes.values()
            ),
            "discovery_sprint_lanes": bool(complete_sprint_ids),
            "discovery_sprint_summarizer": bool(sprint_summarizer_calls),
        },
        "denied_access_attempts": denied_attempts,
        "successful_access_attempts": successful_attempts,
        "incomplete_access_attempts": incomplete_attempts,
    }
    if issues:
        return _result(
            "isolated_access",
            ScenarioStatus.FAIL,
            "An isolated worker was configured with, or successfully exercised, project-memory access.",
            issues,
            evidence,
        )
    if isolated_tasks or denied_attempts or sprint_summarizer_calls:
        return _result(
            "isolated_access",
            ScenarioStatus.PASS,
            "Observed isolated tasks are sealed and no project-memory read succeeded.",
            evidence=evidence,
        )
    return _result(
        "isolated_access",
        ScenarioStatus.INCONCLUSIVE,
        "No brainstorm, multi-discipline, or sealed sprint-lane observation was available.",
        evidence=evidence,
    )


def _category_change_evidence(snapshot: Mapping[str, Any]) -> list[str]:
    state = snapshot.get("categories")
    if not isinstance(state, Mapping):
        return []
    changes: list[str] = []
    history = state.get("category_history", {})
    if isinstance(history, Mapping):
        for category_id, revisions in history.items():
            if isinstance(revisions, list) and revisions:
                changes.append(f"history:{category_id}")
    categories = state.get("categories", {})
    if isinstance(categories, Mapping):
        for category_id, category in categories.items():
            if not isinstance(category, Mapping):
                continue
            if category.get("status") not in {None, "active"}:
                changes.append(f"status:{category_id}:{category.get('status')}")
            if category.get("superseded_by"):
                changes.append(f"superseded:{category_id}")
    return changes


def _evaluate_category_redraw(context: _EvaluationContext) -> ScenarioResult:
    triggers: list[str] = []
    actions = _category_change_evidence(context.snapshot)
    trim_completed = False
    for event in context.events:
        if event.kind in {"category_balance_review", "trim_balance_review"}:
            verdict = _normalized_name(_field(event, "verdict", "balance"))
            if verdict in {"unbalanced", "materially_unbalanced"}:
                triggers.append(f"{event.source}:{event.ordinal}")
        if event.kind == "stuck_report_received" and _field(event, "unbalanced") is True:
            triggers.append(f"{event.source}:{event.ordinal}")
        if event.kind in {
            "category_created",
            "category_updated",
            "category_split",
            "category_merged",
            "category_membership_changed",
            "category_redraw",
        }:
            actions.append(f"event:{event.kind}")
        if event.kind == "category_proposal_committed":
            category_ids = [
                str(item) for item in _list_field(event, "category_ids") if str(item)
            ]
            # A proposal may commit only a portfolio selection.  Count the
            # durable event as redraw evidence only when the category store
            # reports that at least one category was actually touched.
            if category_ids:
                actions.append(
                    "event:category_proposal_committed:" + ",".join(category_ids)
                )
        if event.kind == "trim_committed":
            trim_completed = True
            changes = _field(event, "maintain_changes", "category_changes")
            if changes:
                actions.append("trim_committed_with_maintain_changes")

    scheduler_trim = context.scheduler.get("trim")
    active = False
    if isinstance(scheduler_trim, Mapping):
        active_review = scheduler_trim.get("active_review")
        active_trim = scheduler_trim.get("active_trim")
        active = bool(active_review or active_trim)
        for value in (active_review, active_trim, scheduler_trim.get("last_review")):
            if not isinstance(value, Mapping):
                continue
            report = value.get("report")
            if isinstance(report, Mapping) and report.get("unbalanced") is True:
                triggers.append("scheduler.trim report")
        last_trim = scheduler_trim.get("last_trim")
        if isinstance(last_trim, Mapping):
            trim_completed = True

    evidence = {
        "qualitative_unbalanced_triggers": sorted(set(triggers)),
        "redraw_evidence": sorted(set(actions)),
        "trim_in_progress": active,
        "policy": "no category-size formula or forced split threshold is applied",
    }
    if not triggers:
        return _result(
            "category_redraw",
            ScenarioStatus.INCONCLUSIVE,
            "No semantic observation declared the categories materially unbalanced.",
            (
                "Balance is not inferred from member counts or a forced category split.",
            ),
            evidence,
        )
    if actions:
        return _result(
            "category_redraw",
            ScenarioStatus.PASS,
            "A qualitative imbalance signal is followed by category-maintenance/redraw evidence.",
            evidence=evidence,
        )
    if active and not trim_completed:
        return _result(
            "category_redraw",
            ScenarioStatus.INCONCLUSIVE,
            "A qualitative imbalance was recorded, but the maintain/select cycle is still active.",
            evidence=evidence,
        )
    return _result(
        "category_redraw",
        ScenarioStatus.FAIL,
        "A completed trim followed a declared material imbalance without category redraw evidence.",
        ("No create, revise, membership, merge, split, or explicit retain/redraw rationale was observed.",),
        evidence,
    )


def _evaluate_fact_repair(context: _EvaluationContext) -> ScenarioResult:
    lineages = context.scheduler.get("fact_lineages", {})
    if not isinstance(lineages, Mapping):
        return _result(
            "fact_repair",
            ScenarioStatus.FAIL,
            "scheduler.fact_lineages is malformed.",
        )
    operations = context.scheduler.get("operations", {})
    if not isinstance(operations, Mapping):
        operations = {}
    repaired: list[str] = []
    pending: list[str] = []
    issues: list[str] = []
    observed: dict[str, Any] = {}
    for candidate_id, lineage in lineages.items():
        if not isinstance(lineage, Mapping):
            issues.append(f"fact lineage {candidate_id} is malformed")
            continue
        digests = [str(item) for item in lineage.get("rejected_bundle_digests", [])]
        requests = lineage.get("revision_requests", 0)
        if not digests:
            continue
        if not isinstance(requests, int):
            issues.append(f"fact lineage {candidate_id} has a noninteger revision count")
            continue
        if requests > 2:
            issues.append(f"fact lineage {candidate_id} exceeds the explicit two-request repair limit")
        if len(set(digests)) != len(digests):
            issues.append(f"fact lineage {candidate_id} rechecked an identical rejected bundle")
        if requests != min(len(digests), 2):
            issues.append(
                f"fact lineage {candidate_id} revision count does not match its mathematical rejections"
            )
        concession = bool(lineage.get("concession_required"))
        if len(digests) > 2 and not concession:
            issues.append(f"fact lineage {candidate_id} did not open mandatory concession")
        versions = lineage.get("versions", {})
        version_operations = []
        if isinstance(versions, Mapping):
            version_operations = [operations.get(operation_id) for operation_id in versions.values()]
        rejected_operations = [
            operation
            for operation in version_operations
            if isinstance(operation, Mapping) and operation.get("state") == "rejected"
        ]
        for operation in rejected_operations:
            report = operation.get("verifier_report")
            if not isinstance(report, Mapping) or report.get("verdict") != "incorrect":
                issues.append(
                    f"rejected fact operation {operation.get('operation_id')} lacks an incorrect verifier report"
                )

        task_id = str(lineage.get("task_id") or "")
        task = context.tasks.get(task_id)
        attempts = task.get("attempts", []) if isinstance(task, Mapping) else []
        if not isinstance(attempts, list):
            attempts = []
        revision_attempts = [
            attempt
            for attempt in attempts
            if isinstance(attempt, Mapping)
            and (
                attempt.get("kind") == "verifier_revision"
                or (
                    isinstance(attempt.get("supplement"), Mapping)
                    and attempt["supplement"].get("kind") == "verifier_revision"
                )
            )
        ]
        pending_supplement = (
            task.get("pending_attempt_supplement") if isinstance(task, Mapping) else None
        )
        pending_revision = (
            isinstance(pending_supplement, Mapping)
            and pending_supplement.get("kind") == "verifier_revision"
        )
        if len(revision_attempts) + int(pending_revision) < requests:
            issues.append(
                f"fact lineage {candidate_id} lacks a worker revision attempt for every verifier request"
            )
        closed = bool(lineage.get("closed"))
        terminal = False
        if concession:
            memos = [
                operation
                for operation in operations.values()
                if isinstance(operation, Mapping)
                and operation.get("task_id") == task_id
                and operation.get("kind") == "memo"
                and operation.get("state") == "committed"
            ]
            if closed and not memos:
                issues.append(f"conceded lineage {candidate_id} closed without a committed failure memo")
            terminal = closed and bool(memos)
            if not closed and isinstance(task, Mapping):
                concession_attempt = any(
                    isinstance(attempt, Mapping)
                    and (
                        attempt.get("kind")
                        in {"fact_concession", "concession_memo_correction"}
                        or (
                            isinstance(attempt.get("supplement"), Mapping)
                            and attempt["supplement"].get("kind")
                            in {"fact_concession", "concession_memo_correction"}
                        )
                    )
                    for attempt in attempts
                )
                kind = (
                    pending_supplement.get("kind")
                    if isinstance(pending_supplement, Mapping)
                    else None
                )
                if kind not in {"fact_concession", "concession_memo_correction"}:
                    if not concession_attempt:
                        issues.append(
                            f"lineage {candidate_id} requires concession but no concession attempt exists"
                        )
        else:
            published = any(
                isinstance(operation, Mapping)
                and operation.get("state") == "committed"
                and operation.get("canonical_id")
                and operation.get("verified_correct")
                for operation in version_operations
            )
            terminal = closed and published
        if terminal:
            repaired.append(str(candidate_id))
        else:
            pending.append(str(candidate_id))
        observed[str(candidate_id)] = {
            "rejected_bundles": len(digests),
            "revision_requests": requests,
            "concession_required": concession,
            "closed": closed,
        }

    if issues:
        return _result(
            "fact_repair",
            ScenarioStatus.FAIL,
            "The verifier-triggered repair or concession loop violates its durable invariants.",
            issues,
            {"lineages": observed, "terminal_repairs": repaired, "pending_repairs": pending},
        )
    if repaired and not pending:
        return _result(
            "fact_repair",
            ScenarioStatus.PASS,
            "Rejected proof lineages reached a verified repair or the mandatory concession workflow.",
            evidence={"lineages": observed, "terminal_repairs": repaired},
        )
    if repaired or pending:
        return _result(
            "fact_repair",
            ScenarioStatus.INCONCLUSIVE,
            "The observed repair loop is mechanically valid but at least one lineage remains in progress.",
            evidence={"lineages": observed, "terminal_repairs": repaired, "pending_repairs": pending},
        )
    return _result(
        "fact_repair",
        ScenarioStatus.INCONCLUSIVE,
        "No verifier-incorrect fact lineage was present in the observation.",
    )


def _evaluate_restart_resume(context: _EvaluationContext) -> ScenarioResult:
    indicators: list[str] = []
    issues: list[str] = []
    pending: list[str] = []
    resumed: list[str] = []
    for call_id, call in context.calls.items():
        if not isinstance(call, Mapping):
            continue
        fenced = [int(item) for item in call.get("fenced_epochs", [])]
        epoch = call.get("lease_epoch")
        if fenced:
            indicators.append(f"fenced:{call_id}")
            if not isinstance(epoch, int) or max(fenced) >= epoch or epoch in fenced:
                issues.append(f"call {call_id} does not fence old epochs below its current lease")
            elif call.get("status") in {"running", "completed", "committed"}:
                resumed.append(f"call:{call_id}:post-fence")
        if call.get("status") == "retry_pending":
            pending.append(f"call:{call_id}")
    for task_id, task in context.tasks.items():
        if not isinstance(task, Mapping):
            continue
        attempts = task.get("attempts", [])
        if not isinstance(attempts, list):
            continue
        interrupted = [
            index
            for index, attempt in enumerate(attempts)
            if isinstance(attempt, Mapping) and attempt.get("state") == "interrupted"
        ]
        if interrupted:
            indicators.append(f"interrupted:{task_id}")
        for index in interrupted:
            if index + 1 < len(attempts):
                resumed.append(f"task:{task_id}:attempt:{index + 2}")
        if task.get("state") == "retry_pending":
            pending.append(f"task:{task_id}")

    for event in context.events:
        if event.kind in {
            "call_lease_fenced",
            "lost_worker_reconciled",
            "stale_call_output_rejected",
            "worker_interrupted",
            "call_transport_failure",
        }:
            indicators.append(f"event:{event.kind}")
        if event.kind in {
            "stale_call_output_accepted",
            "recovery_failed",
            "resume_without_persisted_thread",
        }:
            issues.append(str(_field(event, "reason", "summary") or event.kind))
        if event.kind == "transport_call_started" and _field(
            event, "resumed"
        ) is True:
            resumed.append(f"call:{_field(event, 'call_id') or '?'}")
        if event.kind == "thread_binding":
            indicators.append("persisted_thread_binding")

    evidence = {
        "recovery_indicators": sorted(set(indicators)),
        "resumed_work": sorted(set(resumed)),
        "still_pending": sorted(set(pending)),
    }
    if issues:
        return _result(
            "restart_resume",
            ScenarioStatus.FAIL,
            "Restart observations show stale-work acceptance or invalid lease fencing.",
            issues,
            evidence,
        )
    if not indicators:
        return _result(
            "restart_resume",
            ScenarioStatus.INCONCLUSIVE,
            "No interruption, fencing, or resumed-session evidence was observed.",
            evidence=evidence,
        )
    if pending:
        return _result(
            "restart_resume",
            ScenarioStatus.INCONCLUSIVE,
            "Lost work was fenced and preserved, but the observation ends before relaunch/resume.",
            evidence=evidence,
        )
    return _result(
        "restart_resume",
        ScenarioStatus.PASS,
        "Lost work is fenced, durable task identity is retained, and restart/resume evidence is present.",
        evidence=evidence,
    )


def _scheduler_events(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    events = state.get("events", [])
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, Mapping)]


def _evaluate_root_stopping(context: _EvaluationContext) -> ScenarioResult:
    state = context.scheduler
    root = state.get("root", {}) if isinstance(state.get("root"), Mapping) else {}
    events = _scheduler_events(state)
    resolution_events = [item for item in events if item.get("type") == "root_resolution_first"]
    solution_fact_id = root.get("solution_fact_id")
    if not resolution_events and not solution_fact_id:
        return _result(
            "root_stopping",
            ScenarioStatus.INCONCLUSIVE,
            "No active proved/disproved root resolution was observed.",
        )
    resolution_event_id = (
        int(resolution_events[-1].get("event_id", 0)) if resolution_events else 0
    )
    if resolution_events:
        fact_id = (resolution_events[-1].get("payload") or {}).get("fact_id")
        later_revocations = [
            item
            for item in events
            if int(item.get("event_id", 0)) > resolution_event_id
            and item.get("type") == "fact_revocation_applied"
            and fact_id
            in set((item.get("payload") or {}).get("affected_ids", []))
        ]
        if later_revocations and not solution_fact_id:
            return _result(
                "root_stopping",
                ScenarioStatus.INCONCLUSIVE,
                "The observed root resolution was later revoked; research may correctly reopen.",
                evidence={"revocation_events": len(later_revocations)},
            )

    issues: list[str] = []
    gate = str(state.get("gate") or "")
    if solution_fact_id and gate not in {"resolution_pending", "completed"}:
        issues.append(f"active root solution exists while assignment gate is {gate!r}")
    proof_writers = [
        str(task_id)
        for task_id, task in context.tasks.items()
        if isinstance(task, Mapping) and _task_mode(task) == "proof_writer"
    ]
    if len(proof_writers) > 1:
        issues.append(f"more than one proof-writer task exists: {proof_writers}")
    post_resolution_launches: list[str] = []
    for event in events:
        if int(event.get("event_id", 0)) <= resolution_event_id:
            continue
        if event.get("type") != "task_launch_intent_committed":
            continue
        task_id = str((event.get("payload") or {}).get("task_id") or "")
        task = context.tasks.get(task_id)
        mode = _task_mode(task) if isinstance(task, Mapping) else ""
        if mode != "proof_writer":
            post_resolution_launches.append(task_id or "?")
    if post_resolution_launches:
        issues.append(
            f"non-proof research tasks launched after root resolution: {post_resolution_launches}"
        )
    if gate == "completed":
        open_tasks = [
            str(task_id)
            for task_id, task in context.tasks.items()
            if isinstance(task, Mapping) and task.get("state") != "closed"
        ]
        if open_tasks:
            issues.append(f"project completed while tasks remain open: {open_tasks}")
        if not root.get("terminal_main_decision_done"):
            issues.append("project completed before the terminal main-agent decision")

    evidence = {
        "gate": gate,
        "solution_fact_id": solution_fact_id,
        "outcome": root.get("outcome"),
        "terminal_main_decision_done": root.get("terminal_main_decision_done"),
        "proof_writer_task_ids": proof_writers,
        "post_resolution_research_launches": post_resolution_launches,
    }
    if issues:
        return _result(
            "root_stopping",
            ScenarioStatus.FAIL,
            "Research did not stop cleanly after a verified root resolution.",
            issues,
            evidence,
        )
    if not root.get("terminal_main_decision_done"):
        return _result(
            "root_stopping",
            ScenarioStatus.INCONCLUSIVE,
            "The gate blocks research, but the terminal main-agent decision has not yet completed.",
            evidence=evidence,
        )
    return _result(
        "root_stopping",
        ScenarioStatus.PASS,
        "The root resolution closed research assignment, allowed at most one proof-writer, and drains before completion.",
        evidence=evidence,
    )


def _collect_snapshot_events(
    snapshot: Mapping[str, Any],
    extra_events: Iterable[Mapping[str, Any] | ObservationEvent],
) -> ParsedObservations:
    parser = LiveSessionObservationParser()
    events: list[ObservationEvent] = []
    diagnostics: list[str] = []
    sources = (
        ("events", snapshot.get("events", [])),
        ("read_audit", snapshot.get("read_audit", [])),
        ("audit_events", snapshot.get("audit_events", [])),
        ("skill_activity", snapshot.get("skill_activity", [])),
        ("tool_activity", snapshot.get("tool_activity", [])),
        ("live_events", snapshot.get("live_events", [])),
    )
    scheduler = snapshot.get("scheduler")
    if isinstance(scheduler, Mapping):
        sources += (("scheduler.events", scheduler.get("events", [])),)
    categories = snapshot.get("categories")
    if isinstance(categories, Mapping):
        sources += (("categories.events", categories.get("events", [])),)
    for source, values in sources:
        if values is None:
            continue
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            diagnostics.append(f"snapshot.{source} is not a list")
            continue
        parsed = parser.parse_objects(values, source=source)
        events.extend(parsed.events)
        diagnostics.extend(parsed.diagnostics)
    parsed_extra = parser.parse_objects(extra_events, source="supplied-events")
    events.extend(parsed_extra.events)
    diagnostics.extend(parsed_extra.diagnostics)
    return ParsedObservations(tuple(events), tuple(diagnostics))


def evaluate_snapshot(
    snapshot: Mapping[str, Any],
    events: Iterable[Mapping[str, Any] | ObservationEvent] = (),
    *,
    source: str | None = None,
    diagnostics: Iterable[str] = (),
) -> EvaluationReport:
    """Evaluate a JSON-compatible snapshot and return a JSON-ready report."""

    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    parsed = _collect_snapshot_events(snapshot, events)
    context = _EvaluationContext(snapshot, parsed.events)
    scenarios = (
        _evaluate_scheduler_state(context),
        _evaluate_memory_health(context),
        _evaluate_skill_use(context),
        _evaluate_progressive_disclosure(context),
        _evaluate_isolated_access(context),
        _evaluate_category_redraw(context),
        _evaluate_fact_repair(context),
        _evaluate_restart_resume(context),
        _evaluate_root_stopping(context),
    )
    return EvaluationReport(
        scenarios=scenarios,
        diagnostics=tuple(diagnostics) + parsed.diagnostics,
        source=source,
    )


def evaluate_files(
    snapshot_path: str | os.PathLike[str],
    event_paths: Iterable[str | os.PathLike[str]] = (),
) -> EvaluationReport:
    """Read a JSON snapshot plus optional live JSONL logs and evaluate them."""

    path = Path(snapshot_path)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load evaluation snapshot {path}: {exc}") from exc
    if not isinstance(snapshot, Mapping):
        raise ValueError("evaluation snapshot must contain a JSON object")
    parser = LiveSessionObservationParser()
    events: list[ObservationEvent] = []
    diagnostics: list[str] = []
    for event_path in event_paths:
        parsed = parser.parse_path(event_path)
        events.extend(parsed.events)
        diagnostics.extend(parsed.diagnostics)
    return evaluate_snapshot(
        snapshot,
        events,
        source=str(path),
        diagnostics=diagnostics,
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _load_project_database(database: Path) -> tuple[dict[str, Any], list[str]]:
    snapshot: dict[str, Any] = {}
    diagnostics: list[str] = []
    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return snapshot, [f"{database}: cannot open read-only database ({exc})"]
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        if _table_exists(connection, "control_state"):
            rows = connection.execute(
                "SELECT state_key,payload_json FROM control_state "
                "WHERE state_key IN ('scheduler.v1','categories.v1')"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError as exc:
                    diagnostics.append(
                        f"{database}: invalid {row['state_key']} JSON ({exc})"
                    )
                    continue
                if row["state_key"] == "scheduler.v1":
                    snapshot["scheduler"] = payload
                elif row["state_key"] == "categories.v1":
                    snapshot["categories"] = payload
        if _table_exists(connection, "memories"):
            rows = connection.execute(
                "SELECT memory_id,memory_type,revision,active,abstract,core_json,metadata_json "
                "FROM memories ORDER BY memory_type,memory_id"
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                try:
                    core = json.loads(row["core_json"])
                    metadata = json.loads(row["metadata_json"])
                except json.JSONDecodeError as exc:
                    diagnostics.append(
                        f"{database}: invalid memory JSON for {row['memory_id']} ({exc})"
                    )
                    continue
                records.append(
                    {
                        "id": row["memory_id"],
                        "type": row["memory_type"],
                        "revision": row["revision"],
                        "active": bool(row["active"]),
                        "abstract": row["abstract"],
                        **core,
                        **metadata,
                    }
                )
            snapshot["memories"] = records
        if _table_exists(connection, "read_audit"):
            rows = connection.execute(
                "SELECT * FROM read_audit ORDER BY audit_seq"
            ).fetchall()
            snapshot["read_audit"] = [
                {
                    "audit_seq": row["audit_seq"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "policy_label": row["policy_label"],
                    "subject_id": row["subject_id"],
                    "query": json.loads(row["query_json"])
                    if row["query_json"]
                    else None,
                    "result_ids": json.loads(row["result_ids_json"])
                    if row["result_ids_json"]
                    else None,
                    "allowed": bool(row["allowed"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        if _table_exists(connection, "audit_events"):
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY event_seq"
            ).fetchall()
            snapshot["audit_events"] = [
                {
                    "event_seq": row["event_seq"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "subject_id": row["subject_id"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        diagnostics.append(f"{database}: read-only inspection failed ({exc})")
    finally:
        connection.close()
    return snapshot, diagnostics


def _safe_log_paths(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for directory in (root / "audit", root / "private", root / "workspaces"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.rglob("*.jsonl"):
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if path.is_symlink() or not resolved.is_file() or root not in resolved.parents:
                continue
            candidates.add(resolved)
    return sorted(candidates)


def evaluate_project(
    project_root: str | os.PathLike[str],
    *,
    database_path: str | os.PathLike[str] | None = None,
    event_paths: Iterable[str | os.PathLike[str]] = (),
) -> EvaluationReport:
    """Inspect a project using read-only SQLite and file operations."""

    root = Path(project_root).resolve()
    database = Path(database_path).resolve() if database_path else root / "scheduler.sqlite3"
    snapshot, diagnostics = _load_project_database(database)
    parser = LiveSessionObservationParser()
    events: list[ObservationEvent] = []
    paths = _safe_log_paths(root)
    paths.extend(Path(path).resolve() for path in event_paths)
    for path in dict.fromkeys(paths):
        parsed = parser.parse_path(path)
        events.extend(parsed.events)
        diagnostics.extend(parsed.diagnostics)
    return evaluate_snapshot(
        snapshot,
        events,
        source=str(root),
        diagnostics=diagnostics,
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a read-only Franta evaluation report")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", help="JSON evaluation snapshot")
    source.add_argument("--project", help="Franta project directory")
    parser.add_argument("--database", help="database path when --project is used")
    parser.add_argument("--events", action="append", default=[], help="additional JSONL log")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: Sequence[str] | None = None, *, output: TextIO | None = None) -> int:
    """CLI entry point; returns nonzero only when a scenario fails."""

    arguments = _build_argument_parser().parse_args(argv)
    if arguments.snapshot:
        report = evaluate_files(arguments.snapshot, arguments.events)
    else:
        report = evaluate_project(
            arguments.project,
            database_path=arguments.database,
            event_paths=arguments.events,
        )
    stream = output or sys.stdout
    stream.write(report.to_json(indent=None if arguments.compact else 2) + "\n")
    return 1 if report.status is ScenarioStatus.FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EvaluationReport",
    "LiveSessionObservationParser",
    "ObservationEvent",
    "ParsedObservations",
    "ScenarioResult",
    "ScenarioStatus",
    "evaluate_files",
    "evaluate_project",
    "evaluate_snapshot",
    "main",
    "parse_live_session",
]
