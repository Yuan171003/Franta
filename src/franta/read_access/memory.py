"""Policy-filtered project-memory search and complete-record reads."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable, Protocol, Sequence

from ..contracts.agent_access import AccessPolicy, MemoryRecord, _ID_RE
from .audit import AuditLog


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class AccessError(RuntimeError):
    """An agent attempted an operation outside its launch-bound policy."""


class MemoryBackend(Protocol):
    def iter_records(self, memory_types: frozenset[str]) -> Iterable[MemoryRecord]: ...

    def get_record(self, memory_id: str) -> MemoryRecord | None: ...


class InMemoryBackend:
    """Small deterministic backend useful for tests and broker embedding."""

    def __init__(self, records: Iterable[MemoryRecord] = ()) -> None:
        self._records = {record.memory_id: record for record in records}

    def iter_records(self, memory_types: frozenset[str]) -> Iterable[MemoryRecord]:
        return (
            record
            for record in self._records.values()
            if record.memory_type in memory_types
        )

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    def put(self, record: MemoryRecord) -> None:
        self._records[record.memory_id] = record


def _tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(text)]


def _bm25(query: str, records: Sequence[MemoryRecord]) -> list[tuple[MemoryRecord, float]]:
    query_terms = _tokens(query)
    if not query_terms:
        raise AccessError("internal-search requires a nonempty mathematical query")
    if not records:
        return []
    documents = [Counter(_tokens(f"{r.title} {r.abstract}")) for r in records]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 1.0
    document_frequency = {
        term: sum(1 for document in documents if term in document)
        for term in set(query_terms)
    }
    scored: list[tuple[MemoryRecord, float]] = []
    for record, document, length in zip(records, documents, lengths):
        score = 0.0
        for term in query_terms:
            frequency = document.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(
                1.0 + (len(records) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * length / average)
            score += inverse * frequency * 2.5 / denominator
        if score > 0:
            scored.append((record, score))
    return sorted(scored, key=lambda item: (-item[1], item[0].memory_id))


class AuditedMemoryAPI:
    """Policy-filtered search/fetch facade; the canonical backend stays private."""

    def __init__(
        self,
        backend: MemoryBackend,
        policy: AccessPolicy,
        *,
        audit: AuditLog | None = None,
        caller_id: str = "unknown",
        root_fact_id: str | None = None,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.audit = audit or AuditLog()
        self.caller_id = caller_id
        self.root_fact_id = root_fact_id
        self._closure_grants: set[str] = {root_fact_id} if root_fact_id else set()

    def search(
        self,
        query: str,
        memory_types: Iterable[str],
        *,
        limit: int = 10,
        include_inactive: bool = False,
        include_withdrawn: bool = False,
    ) -> list[dict[str, Any]]:
        requested = frozenset(memory_types)
        if not self.policy.project_memory_api:
            raise AccessError("internal-search is unavailable for this call")
        if not self.policy.permits_types(requested):
            raise AccessError("requested memory types exceed the launch-bound policy")
        if not 1 <= limit <= 10:
            raise AccessError("internal-search limit must be between 1 and 10")

        # Access and status filtering intentionally happens before ranking.
        visible: list[MemoryRecord] = []
        for record in self.backend.iter_records(requested):
            if record.memory_type == "fact" and not record.active and not include_inactive:
                continue
            if record.memory_type == "claim" and record.withdrawn and not include_withdrawn:
                continue
            visible.append(record)
        ranked = _bm25(query, visible)[:limit]
        result = [record.summary(score=round(score, 8)) for record, score in ranked]
        self.audit.append(
            {
                "action": "internal_search",
                "caller_id": self.caller_id,
                "policy": self.policy.name,
                "query": query,
                "memory_types": sorted(requested),
                "limit": limit,
                "result_ids": [entry["id"] for entry in result],
            }
        )
        return result

    def fetch(self, memory_id: str) -> dict[str, Any]:
        if not _ID_RE.fullmatch(memory_id):
            raise AccessError("memory_fetch accepts a canonical ID, not a path")
        record = self.backend.get_record(memory_id)
        if record is None:
            raise AccessError(f"unknown memory ID: {memory_id}")
        if record.memory_type not in self.policy.allowed_memory_types:
            raise AccessError("record type exceeds the launch-bound policy")
        if self.policy.dependency_closure_only and memory_id not in self._closure_grants:
            raise AccessError("fact is outside the root solution dependency closure")
        if not (self.policy.project_memory_api or self.policy.dependency_closure_only):
            raise AccessError("complete project-memory fetch is unavailable for this call")
        if record.memory_type == "fact" and not record.active:
            raise AccessError("inactive facts cannot be supplied as established premises")
        self.audit.append(
            {
                "action": "memory_fetch",
                "caller_id": self.caller_id,
                "policy": self.policy.name,
                "memory_id": memory_id,
            }
        )
        return record.full()

    def dependency_closure(self) -> list[dict[str, Any]]:
        """Grant and return abstracts for the active root-fact dependency closure."""

        if not self.policy.dependency_closure_only or not self.root_fact_id:
            raise AccessError("fact dependency closure is unavailable for this call")
        pending = [self.root_fact_id]
        closure: dict[str, MemoryRecord] = {}
        while pending:
            memory_id = pending.pop()
            if memory_id in closure:
                continue
            record = self.backend.get_record(memory_id)
            if record is None or record.memory_type != "fact" or not record.active:
                raise AccessError(f"root proof closure contains unavailable fact {memory_id}")
            closure[memory_id] = record
            pending.extend(record.predecessor_ids)
        self._closure_grants.update(closure)
        result = [closure[key].summary() for key in sorted(closure)]
        self.audit.append(
            {
                "action": "fact_dependency_closure",
                "caller_id": self.caller_id,
                "policy": self.policy.name,
                "root_fact_id": self.root_fact_id,
                "result_ids": sorted(closure),
                "portfolio_limit_exempt": True,
            }
        )
        return result


__all__ = [
    "AccessError",
    "AuditedMemoryAPI",
    "InMemoryBackend",
    "MemoryBackend",
]
