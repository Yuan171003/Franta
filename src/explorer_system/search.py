"""Portable audited joint search over host and Explorer memory."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Any, Callable, Iterable, Protocol, Sequence

from .contracts import (
    EXPLORER_RECORD_ID_RE,
    EXPLORER_SEARCH_TYPES,
    ExplorerAccessError,
    ExplorerReadScope,
)
from .repository import ExplorerRepository


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class CanonicalRecordPort(Protocol):
    memory_id: str
    memory_type: str
    title: str | None
    abstract: str
    active: bool
    withdrawn: bool

    def summary(self, *, score: float | None = None) -> dict[str, Any]: ...

    def full(self) -> dict[str, Any]: ...


class CanonicalMemoryPort(Protocol):
    def iter_records(
        self, memory_types: frozenset[str]
    ) -> Iterable[CanonicalRecordPort]: ...

    def get_record(self, memory_id: str) -> CanonicalRecordPort | None: ...


class CanonicalAccessPort(Protocol):
    name: str
    explorer_memory_api: bool
    project_memory_api: bool
    allowed_memory_types: frozenset[str]

    def permits_types(self, requested: Iterable[str]) -> bool: ...


class AuditPort(Protocol):
    def append(self, event: dict[str, Any]) -> None: ...


class _NullAudit:
    def append(self, event: dict[str, Any]) -> None:
        del event


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value)]


@dataclass(frozen=True)
class _Candidate:
    record_id: str
    title: str
    abstract: str
    render: Callable[[float], dict[str, Any]]


def _rank(
    query: str,
    candidates: Sequence[_Candidate],
    *,
    access_error: type[Exception] = ExplorerAccessError,
) -> list[tuple[_Candidate, float]]:
    query_terms = _tokens(query)
    if not query_terms:
        raise access_error("explorer-search requires a nonempty mathematical query")
    if not candidates:
        return []
    documents = [Counter(_tokens(f"{item.title} {item.abstract}")) for item in candidates]
    lengths = [sum(document.values()) for document in documents]
    average_length = max(sum(lengths) / len(lengths), 1.0)
    frequencies = {
        term: sum(1 for document in documents if term in document)
        for term in set(query_terms)
    }
    scored: list[tuple[_Candidate, float]] = []
    for candidate, document, length in zip(candidates, documents, lengths):
        score = 0.0
        for term in query_terms:
            frequency = document.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(
                1.0
                + (len(candidates) - frequencies[term] + 0.5)
                / (frequencies[term] + 0.5)
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * length / average_length
            )
            score += inverse * frequency * 2.5 / denominator
        if score > 0:
            scored.append((candidate, score))
    return sorted(scored, key=lambda item: (-item[1], item[0].record_id))


class AuditedExplorerAPI:
    """Policy-bound joint search and explicit full-record fetch.

    The canonical backend remains read-only and unchanged.  Explorer visibility
    is independently constrained by a server-owned turn/session/high-water
    scope.  Results always identify their record space so provisional scratch
    cannot masquerade as an established Fact.
    """

    def __init__(
        self,
        canonical_backend: CanonicalMemoryPort,
        repository: ExplorerRepository,
        canonical_policy: CanonicalAccessPort,
        explorer_scope: ExplorerReadScope,
        *,
        audit: AuditPort | None = None,
        caller_id: str = "unknown",
        canonical_search_types: frozenset[str],
        canonical_id_pattern: re.Pattern[str],
        access_error: type[Exception] = ExplorerAccessError,
    ) -> None:
        self.access_error = access_error
        if not canonical_policy.explorer_memory_api:
            raise self.access_error(
                "Explorer API requires an Explorer-enabled launch-bound policy"
            )
        self.canonical_backend = canonical_backend
        self.repository = repository
        self.canonical_policy = canonical_policy
        self.explorer_scope = explorer_scope
        self.audit = audit or _NullAudit()
        self.caller_id = caller_id
        self.canonical_search_types = frozenset(canonical_search_types)
        self.canonical_id_pattern = canonical_id_pattern

    @staticmethod
    def _canonical_candidate(record: CanonicalRecordPort) -> _Candidate:
        def render(score: float) -> dict[str, Any]:
            value = record.summary(score=round(score, 8))
            value.update(
                {
                    "record_space": "canonical",
                    "record_type": record.memory_type,
                }
            )
            value.setdefault("status", "current")
            return value

        return _Candidate(
            record_id=record.memory_id,
            title=record.title or record.abstract[:160],
            abstract=record.abstract,
            render=render,
        )

    @staticmethod
    def _explorer_candidate(document: Any) -> _Candidate:
        record = document.record

        def render(score: float) -> dict[str, Any]:
            return record.summary(score=round(score, 8))

        return _Candidate(
            record_id=record.record_id,
            title=record.title,
            abstract=record.abstract,
            render=render,
        )

    def search(
        self,
        query: str,
        record_types: Iterable[str],
        *,
        limit: int = 10,
        include_inactive: bool = False,
        include_withdrawn: bool = False,
    ) -> list[dict[str, Any]]:
        if isinstance(record_types, (str, bytes)):
            raise self.access_error("record_types must be a nonempty list")
        requested = frozenset(record_types)
        joint_types = self.canonical_search_types | EXPLORER_SEARCH_TYPES
        if not requested or not requested <= joint_types:
            raise self.access_error(
                "record_types must be a nonempty canonical/scratch/summary subset"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise self.access_error("explorer-search limit must be between 1 and 10")
        canonical_types = requested & self.canonical_search_types
        explorer_types = requested & EXPLORER_SEARCH_TYPES
        if canonical_types:
            if not self.canonical_policy.project_memory_api:
                raise self.access_error(
                    "canonical project memory is unavailable for this call"
                )
            if not self.canonical_policy.permits_types(canonical_types):
                raise self.access_error(
                    "requested canonical types exceed the launch-bound policy"
                )

        candidates: list[_Candidate] = []
        if canonical_types:
            for record in self.canonical_backend.iter_records(
                frozenset(canonical_types)
            ):
                if record.memory_type == "fact" and not record.active and not include_inactive:
                    continue
                if (
                    record.memory_type == "claim"
                    and record.withdrawn
                    and not include_withdrawn
                ):
                    continue
                candidates.append(self._canonical_candidate(record))
        if explorer_types:
            candidates.extend(
                self._explorer_candidate(document)
                for document in self.repository.search_documents(
                    self.explorer_scope, explorer_types
                )
            )

        selected = _rank(
            query, candidates, access_error=self.access_error
        )[:limit]
        result = [candidate.render(score) for candidate, score in selected]
        self.audit.append(
            {
                "action": "explorer_search",
                "caller_id": self.caller_id,
                "policy": self.canonical_policy.name,
                "explorer_scope": self.explorer_scope.label,
                "query": query,
                "record_types": sorted(requested),
                "limit": limit,
                "result_ids": [entry["id"] for entry in result],
            }
        )
        return result

    explorer_search = search

    def fetch(self, record_id: str) -> dict[str, Any]:
        if not isinstance(record_id, str):
            raise self.access_error("explorer-fetch requires a record ID string")
        if EXPLORER_RECORD_ID_RE.fullmatch(record_id):
            record = self.repository.fetch(self.explorer_scope, record_id)
            if record is None:
                raise self.access_error(
                    f"Explorer record is unavailable: {record_id}"
                )
            result = record.full()
            # Scratch cites the CAS operation ID returned to its worker.  Add
            # the server-owned XCAS identity at fetch time so a later host
            # integrator can request an authenticated computation export
            # without guessing an otherwise undiscoverable registry ID.
            evidence = []
            for item in self.repository.list_cas_evidence_for_record(
                record.record_id
            ):
                evidence.append(
                    {
                        "evidence_id": item.evidence_id,
                        "operation_id": item.operation_id,
                        "execution_succeeded": item.execution_succeeded,
                        "turn_id": item.turn_id,
                        "worker_session_id": item.worker_session_id,
                        "attempt_no": item.attempt_no,
                        "output_artifact_sha256": item.output_artifact_sha256,
                    }
                )
            result["cas_evidence"] = evidence
        elif self.canonical_id_pattern.fullmatch(record_id):
            if not self.canonical_policy.project_memory_api:
                raise self.access_error(
                    "canonical project-memory fetch is unavailable"
                )
            record = self.canonical_backend.get_record(record_id)
            if record is None:
                raise self.access_error(f"unknown canonical memory ID: {record_id}")
            if record.memory_type not in self.canonical_policy.allowed_memory_types:
                raise self.access_error(
                    "record type exceeds the launch-bound policy"
                )
            if record.memory_type == "fact" and not record.active:
                raise self.access_error(
                    "inactive facts cannot be supplied as established premises"
                )
            result = record.full()
            result.update(
                {
                    "record_space": "canonical",
                    "record_type": record.memory_type,
                }
            )
        else:
            raise self.access_error(
                "explorer-fetch accepts a record ID, not a path"
            )
        self.audit.append(
            {
                "action": "explorer_fetch",
                "caller_id": self.caller_id,
                "policy": self.canonical_policy.name,
                "explorer_scope": self.explorer_scope.label,
                "record_id": record_id,
                "record_space": result["record_space"],
            }
        )
        return result

    explorer_fetch = fetch


__all__ = [
    "AuditPort",
    "AuditedExplorerAPI",
    "CanonicalAccessPort",
    "CanonicalMemoryPort",
    "CanonicalRecordPort",
]
