"""Abstract-first BM25-style search over canonical memory."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

from ..contracts.canonical import (
    AccessPolicy,
    MemoryRecord,
    MemoryType,
    SearchResult,
    ValidationError,
)
from ..store import MemoryStore


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_DEFAULT_SEARCH_TYPES = frozenset(
    {
        MemoryType.FACT,
        MemoryType.ROUTE,
        MemoryType.MEMO,
        MemoryType.CLAIM,
        MemoryType.OBLIGATION,
        MemoryType.COMPUTATION,
    }
)


def tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(text)]


class SearchEngine:
    """BM25 ranking whose results expose only summaries and abstracts."""

    def __init__(self, store: MemoryStore, *, k1: float = 1.2, b: float = 0.75) -> None:
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        self.store = store
        self.k1 = float(k1)
        self.b = float(b)

    def search(
        self,
        query: str,
        *,
        types: Iterable[MemoryType | str] | None = None,
        limit: int = 10,
        include_inactive_facts: bool = False,
        include_withdrawn_claims: bool = False,
        include_removed_obligations: bool = False,
        actor: str = "system",
        access_policy: AccessPolicy | None = None,
    ) -> list[SearchResult]:
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("search query must be a nonempty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValidationError("search limit must be between 1 and 10")
        if types is None:
            parsed_types = set(_DEFAULT_SEARCH_TYPES)
        else:
            try:
                parsed_types = {MemoryType(item) for item in types}
            except ValueError as exc:
                raise ValidationError("search contains an unknown memory type") from exc
            if not parsed_types:
                raise ValidationError("search types must be a nonempty subset")
            unsupported = parsed_types - _DEFAULT_SEARCH_TYPES
            if unsupported:
                raise ValidationError(
                    "task memory is read through task summaries, not internal-search"
                )
        policy = access_policy or AccessPolicy(
            allow_inactive_facts=include_inactive_facts,
            allow_withdrawn_claims=include_withdrawn_claims,
            allow_removed_obligations=include_removed_obligations,
            label="search-default",
        )
        candidates = []
        for candidate in self.store._search_candidates(parsed_types):
            kind = candidate["memory_type"]
            status = candidate["status"]
            if kind is MemoryType.FACT and status == "revoked" and not include_inactive_facts:
                continue
            if kind is MemoryType.CLAIM and status == "withdrawn" and not include_withdrawn_claims:
                continue
            if (
                kind is MemoryType.OBLIGATION
                and status == "removed"
                and not include_removed_obligations
            ):
                continue
            if not policy.permits(candidate["memory_id"], kind, status):
                continue
            candidates.append(candidate)

        query_tokens = tokenize(query)
        if not query_tokens or not candidates:
            self.store._audit_search(
                actor=actor,
                policy=policy,
                query={
                    "text": query,
                    "types": sorted(item.value for item in parsed_types),
                    "limit": limit,
                    "include_inactive_facts": include_inactive_facts,
                    "include_withdrawn_claims": include_withdrawn_claims,
                    "include_removed_obligations": include_removed_obligations,
                },
                result_ids=[],
            )
            return []

        token_counts: dict[str, Counter[str]] = {}
        lengths: dict[str, int] = {}
        document_frequency: Counter[str] = Counter()
        for candidate in candidates:
            tokens = tokenize(candidate["search_text"])
            counts = Counter(tokens)
            token_counts[candidate["memory_id"]] = counts
            lengths[candidate["memory_id"]] = len(tokens)
            for token in set(tokens):
                document_frequency[token] += 1
        average_length = sum(lengths.values()) / len(lengths) if lengths else 1.0
        average_length = max(average_length, 1.0)
        query_frequency = Counter(query_tokens)
        scored: list[tuple[float, dict[str, object]]] = []
        document_count = len(candidates)
        for candidate in candidates:
            memory_id = candidate["memory_id"]
            counts = token_counts[memory_id]
            document_length = lengths[memory_id]
            score = 0.0
            for token, qtf in query_frequency.items():
                term_frequency = counts[token]
                if term_frequency == 0:
                    continue
                df = document_frequency[token]
                idf = math.log(1.0 + (document_count - df + 0.5) / (df + 0.5))
                denominator = term_frequency + self.k1 * (
                    1.0 - self.b + self.b * document_length / average_length
                )
                score += idf * (
                    term_frequency * (self.k1 + 1.0) / denominator
                ) * qtf
            if score > 0:
                scored.append((score, candidate))
        scored.sort(key=lambda item: (-item[0], str(item[1]["memory_id"])))
        selected = scored[:limit]
        results = [
            SearchResult(
                memory_id=str(candidate["memory_id"]),
                memory_type=candidate["memory_type"],  # type: ignore[arg-type]
                display_title=str(candidate["title"]),
                abstract=str(candidate["abstract"]),
                status=str(candidate["status"]),
                score=float(score),
                rank=index,
            )
            for index, (score, candidate) in enumerate(selected, start=1)
        ]
        self.store._audit_search(
            actor=actor,
            policy=policy,
            query={
                "text": query,
                "types": sorted(item.value for item in parsed_types),
                "limit": limit,
                "include_inactive_facts": include_inactive_facts,
                "include_withdrawn_claims": include_withdrawn_claims,
                "include_removed_obligations": include_removed_obligations,
            },
            result_ids=[result.memory_id for result in results],
        )
        return results

    def fetch(
        self,
        memory_id: str,
        *,
        actor: str,
        access_policy: AccessPolicy | None = None,
    ) -> MemoryRecord:
        return self.store.fetch(
            memory_id,
            actor=actor,
            access_policy=access_policy,
        )


def search_memory(store: MemoryStore, query: str, **kwargs: object) -> list[SearchResult]:
    return SearchEngine(store).search(query, **kwargs)  # type: ignore[arg-type]


__all__ = ["SearchEngine", "search_memory", "tokenize"]
