"""Portable, mechanically enforced staged-memory access for Explorer.

This module owns the three-attempt access policy.  Host adapters supply one
immutable published-memory projection; Explorer freezes that projection and
its own trusted high-water mark into an append-only attempt grant.  Worker
responses are deliberately narrower than the private audit representation.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import random
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .contracts import (
    ExplorerAccessError,
    ExplorerAttemptAccessGrant,
    ExplorerPortfolioItem,
    ExplorerPortfolioSnapshot,
    ExplorerPublishedMemoryDocument,
    ExplorerPublishedMemorySnapshot,
    ExplorerReadScope,
    ExplorerRecord,
    ExplorerValidationError,
)
from .repository import ExplorerRepository


CHECK_RESULT_KINDS = frozenset({"proved", "disproved", "computed"})
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "every",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)
_OPEN_QUERY_RE = re.compile(
    r"^(?:whether|what|which|who|where|when|why|how|find|show|prove|disprove|"
    r"compute|calculate|determine|decide|is it known|do we know)\b",
    re.IGNORECASE,
)
_PREDICATE_RE = re.compile(
    r"(?:[=<>≤≥∈∉⊂⊆≅]|\b(?:is|are|was|were|has|have|equals?|exists?|"
    r"implies?|contains?|admits?|vanishes?|converges?|diverges?|holds?|"
    r"for\s+all|for\s+every|there\s+is|there\s+exists)\b|"
    r"(?:是|为|存在|等于|对于|任意|蕴含|成立|收敛|发散))",
    re.IGNORECASE,
)


class ExplorerAccessAuditPort(Protocol):
    def append(self, event: dict[str, Any]) -> None: ...


class _NullAudit:
    def append(self, event: dict[str, Any]) -> None:
        del event


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(value)]


def _search_tokens(value: str) -> list[str]:
    return [token for token in _tokens(value) if token not in _SEARCH_STOPWORDS]


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:32]


def validate_check_result_request(kind: str, statement: str) -> tuple[str, str]:
    """Validate one closed check-result request.

    The validator intentionally accepts assertions, not research topics or
    questions.  It is a mechanical guard rather than a theorem prover: the
    prompt and broker still require the assertion to carry its quantifiers and
    hypotheses explicitly.
    """

    if kind not in CHECK_RESULT_KINDS:
        raise ExplorerAccessError(
            "check-result kind must be proved, disproved, or computed"
        )
    if (
        not isinstance(statement, str)
        or not statement
        or statement != statement.strip()
        or len(statement.encode("utf-8")) > 8_192
    ):
        raise ExplorerAccessError(
            "check-result statement must be one exact bounded proposition"
        )
    if (
        "\n" in statement
        or "\r" in statement
        or "?" in statement
        or "；" in statement
        or ";" in statement
        or _OPEN_QUERY_RE.search(statement)
        or re.match(r"^(?:[-*•]|\d+[.)])\s", statement)
    ):
        raise ExplorerAccessError(
            "check-result statement must be one proposition, not a question, "
            "request, or list"
        )
    if not _PREDICATE_RE.search(statement):
        raise ExplorerAccessError(
            "check-result statement must contain a definite mathematical predicate"
        )
    if len(_tokens(statement)) < 2 and not re.search(r"[=<>≤≥∈∉⊂⊆≅]", statement):
        raise ExplorerAccessError(
            "check-result statement is too incomplete to be a strict proposition"
        )
    return kind, statement


def _bm25_scores(
    query: str,
    documents: Sequence[tuple[str, str]],
) -> list[tuple[int, float]]:
    """Return deterministic BM25 scores, retaining zero-score candidates."""

    terms = _search_tokens(query)
    if not terms:
        raise ExplorerValidationError("BM25 query must contain searchable terms")
    if not documents:
        return []
    counters = [Counter(_search_tokens(text)) for _, text in documents]
    lengths = [sum(counter.values()) for counter in counters]
    average = max(sum(lengths) / len(lengths), 1.0)
    frequencies = {
        term: sum(1 for counter in counters if term in counter)
        for term in set(terms)
    }
    result: list[tuple[int, float]] = []
    for index, (counter, length) in enumerate(zip(counters, lengths, strict=True)):
        score = 0.0
        for term in terms:
            frequency = counter.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(
                1.0
                + (len(documents) - frequencies[term] + 0.5)
                / (frequencies[term] + 0.5)
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * length / average
            )
            score += inverse * frequency * 2.5 / denominator
        result.append((index, score))
    return sorted(
        result,
        key=lambda entry: (-entry[1], documents[entry[0]][0]),
    )


def _summary_query(record: ExplorerRecord) -> str:
    """Use only directions tried and main progress, as required by policy."""

    if record.record_type != "summary" or not record.main_progress:
        raise ExplorerValidationError("portfolio source must be a completed summary")
    value = "\n".join((*record.directions_tried, record.main_progress)).strip()
    if not value:
        raise ExplorerValidationError("attempt summary has no portfolio query text")
    return value


def _record_digest(record: ExplorerRecord) -> str:
    return record.input_digest


def _select_closest_and_random(
    candidates: Sequence[Any],
    *,
    query: str,
    text_of: Any,
    id_of: Any,
    closest_count: int,
    random_count: int,
    rng: random.Random,
) -> list[tuple[Any, str]]:
    ordered = sorted(candidates, key=id_of)
    ranked = _bm25_scores(
        query, [(id_of(candidate), text_of(candidate)) for candidate in ordered]
    )
    closest_indexes = [index for index, _ in ranked[:closest_count]]
    selected = [(ordered[index], "closest") for index in closest_indexes]
    remaining = [
        candidate
        for index, candidate in enumerate(ordered)
        if index not in set(closest_indexes)
    ]
    random_take = min(random_count, len(remaining))
    if random_take:
        for candidate in rng.sample(remaining, random_take):
            selected.append((candidate, "random"))
    return selected


def _portfolio_item(
    portfolio_id: str,
    *,
    item_kind: str,
    source_id: str,
    source_digest: str,
    abstract: str,
    main_content: str,
    selection_reason: str,
    worker_session_id: str | None = None,
    attempt_no: int | None = None,
) -> ExplorerPortfolioItem:
    return ExplorerPortfolioItem(
        portfolio_item_id=_identity(
            "XPI-", portfolio_id, item_kind, source_id, source_digest
        ),
        item_kind=item_kind,
        source_id=source_id,
        source_digest=source_digest,
        abstract=abstract,
        main_content=main_content,
        selection_reason=selection_reason,
        source_worker_session_id=worker_session_id,
        source_attempt_no=attempt_no,
    )


def build_attempt_access_grant(
    repository: ExplorerRepository,
    *,
    turn_id: str,
    worker_session_id: str,
    attempt_no: int,
    host_snapshot: ExplorerPublishedMemorySnapshot,
    explorer_high_water_seq: int,
    experiment_seed: str = "default",
    fallback_query: str | None = None,
) -> ExplorerAttemptAccessGrant:
    """Construct one deterministic grant from frozen host/Explorer snapshots."""

    if attempt_no not in {1, 2, 3}:
        raise ExplorerValidationError("Explorer access policy has exactly three attempts")
    for value, field in (
        (turn_id, "turn_id"),
        (worker_session_id, "worker_session_id"),
        (experiment_seed, "experiment_seed"),
    ):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ExplorerValidationError(f"{field} must be nonempty exact text")
    if (
        not isinstance(explorer_high_water_seq, int)
        or isinstance(explorer_high_water_seq, bool)
        or explorer_high_water_seq < 0
    ):
        raise ExplorerValidationError("explorer_high_water_seq must be nonnegative")

    eligible = tuple(document for document in host_snapshot.documents if document.eligible)
    grant_id = _identity("XAG-", turn_id, worker_session_id, attempt_no)
    access_mode = {1: "check-result", 2: "portfolio", 3: "full-memory"}[
        attempt_no
    ]
    portfolio: ExplorerPortfolioSnapshot | None = None

    if attempt_no == 2:
        scope = ExplorerReadScope(
            allowed_turn_ids=frozenset({turn_id}),
            max_seq=explorer_high_water_seq,
            label=f"portfolio-freeze:{turn_id}:{worker_session_id}",
        )
        records = [item.record for item in repository.search_documents(scope)]
        own_summaries = [
            record
            for record in records
            if record.record_type == "summary"
            and record.worker_session_id == worker_session_id
            and record.attempt_no == 1
        ]
        if len(own_summaries) > 1:
            raise ExplorerValidationError(
                "Attempt 2 has more than one trusted Attempt-1 summary"
            )
        own_summary = own_summaries[0] if own_summaries else None
        if own_summary is not None:
            query = _summary_query(own_summary)
            query_source = "attempt-1-summary"
            query_source_ids = (own_summary.record_id,)
            query_source_digests = (own_summary.input_digest,)
            source_summary_id: str | None = own_summary.record_id
            source_summary_digest: str | None = own_summary.input_digest
        else:
            own_query_scratches = sorted(
                (
                    record
                    for record in records
                    if record.record_type == "scratch"
                    and record.worker_session_id == worker_session_id
                    and record.attempt_no == 1
                ),
                key=lambda record: (record.seq, record.record_id),
            )
            if own_query_scratches:
                query = "\n".join(
                    component
                    for record in own_query_scratches
                    for component in (record.abstract, record.content)
                )
                query_source = "attempt-1-scratch-fallback"
                query_source_ids = tuple(
                    record.record_id for record in own_query_scratches
                )
                query_source_digests = tuple(
                    record.input_digest for record in own_query_scratches
                )
                source_summary_id = None
                source_summary_digest = None
            else:
                if (
                    not isinstance(fallback_query, str)
                    or not fallback_query
                    or fallback_query != fallback_query.strip()
                ):
                    raise ExplorerValidationError(
                        "Attempt 2 without a trusted Attempt-1 summary or "
                        "direction/progress scratch requires an exact fallback_query"
                    )
                query = fallback_query
                query_source = "host-fallback"
                query_source_ids = ()
                query_source_digests = ()
                source_summary_id = None
                source_summary_digest = None
        query_anchor_digest = _digest_text(
            "\x1f".join(
                (
                    query_source,
                    query,
                    *query_source_ids,
                    *query_source_digests,
                )
            )
        )
        seed_digest = _digest_text(
            "\x1f".join(
                (
                    experiment_seed,
                    turn_id,
                    worker_session_id,
                    str(attempt_no),
                    query_anchor_digest,
                    str(host_snapshot.source_set_digest),
                    str(explorer_high_water_seq),
                )
            )
        )
        rng = random.Random(int(seed_digest, 16))
        portfolio_id = _identity("XP-", grant_id, seed_digest)
        items: list[ExplorerPortfolioItem] = []

        routes = [item for item in eligible if item.memory_kind == "route"]
        for document, reason in _select_closest_and_random(
            routes,
            query=query,
            text_of=lambda item: item.main_content,
            id_of=lambda item: item.source_id,
            closest_count=1,
            random_count=2,
            rng=rng,
        ):
            items.append(
                _portfolio_item(
                    portfolio_id,
                    item_kind="route",
                    source_id=document.source_id,
                    source_digest=document.source_digest,
                    abstract=document.abstract,
                    main_content=document.main_content,
                    selection_reason=reason,
                )
            )

        memos = [item for item in eligible if item.memory_kind == "memo"]
        for document, reason in _select_closest_and_random(
            memos,
            query=query,
            text_of=lambda item: item.main_content,
            id_of=lambda item: item.source_id,
            closest_count=3,
            random_count=3,
            rng=rng,
        ):
            items.append(
                _portfolio_item(
                    portfolio_id,
                    item_kind="memo",
                    source_id=document.source_id,
                    source_digest=document.source_digest,
                    abstract=document.abstract,
                    main_content=document.main_content,
                    selection_reason=reason,
                )
            )

        peer_summaries = [
            record
            for record in records
            if record.record_type == "summary"
            and record.worker_session_id != worker_session_id
        ]
        peer_summaries = sorted(
            peer_summaries,
            key=lambda record: (record.worker_session_id, record.attempt_no, record.record_id),
        )
        peer_rank = _bm25_scores(
            query,
            [
                (record.record_id, _summary_query(record))
                for record in peer_summaries
            ],
        )
        selected_peers: list[tuple[ExplorerRecord, str]] = []
        if peer_rank:
            closest = peer_summaries[peer_rank[0][0]]
            selected_peers.append((closest, "closest"))
            other_lineages = [
                record
                for record in peer_summaries
                if record.worker_session_id != closest.worker_session_id
            ]
            if other_lineages:
                selected_peers.append((rng.choice(other_lineages), "random"))

        for summary, reason in selected_peers:
            items.append(
                _portfolio_item(
                    portfolio_id,
                    item_kind="peer-summary",
                    source_id=summary.record_id,
                    source_digest=_record_digest(summary),
                    abstract=summary.abstract,
                    main_content=summary.content,
                    selection_reason=reason,
                    worker_session_id=summary.worker_session_id,
                    attempt_no=summary.attempt_no,
                )
            )
            peer_scratches = sorted(
                (
                    record
                    for record in records
                    if record.record_type == "scratch"
                    and record.worker_session_id == summary.worker_session_id
                    and record.attempt_no == summary.attempt_no
                ),
                key=lambda record: (record.seq, record.record_id),
            )
            for scratch in peer_scratches:
                items.append(
                    _portfolio_item(
                        portfolio_id,
                        item_kind="peer-scratch",
                        source_id=scratch.record_id,
                        source_digest=_record_digest(scratch),
                        abstract=scratch.abstract,
                        main_content=scratch.content,
                        selection_reason="peer-member",
                        worker_session_id=scratch.worker_session_id,
                        attempt_no=scratch.attempt_no,
                    )
                )

        portfolio = ExplorerPortfolioSnapshot(
            portfolio_id=portfolio_id,
            query_source=query_source,
            query_source_record_ids=query_source_ids,
            query_source_record_digests=query_source_digests,
            source_summary_id=source_summary_id,
            source_summary_digest=source_summary_digest,
            query_text=query,
            selection_seed=seed_digest,
            host_snapshot_revision=host_snapshot.revision,
            host_snapshot_digest=str(host_snapshot.source_set_digest),
            explorer_high_water_seq=explorer_high_water_seq,
            items=tuple(items),
        )

    # Attempts 1 and 2 persist only the eligible corpus needed by
    # check-result. Attempt 3 persists the complete host projection, including
    # ineligible records, so an adapter can replay the exact launch snapshot
    # for full-memory search/fetch across retries and restarts.
    published_documents = (
        tuple(
            document
            for document in eligible
            if document.memory_kind in {"fact", "claim", "computation"}
        )
        if attempt_no in {1, 2}
        else tuple(host_snapshot.documents)
    )
    return ExplorerAttemptAccessGrant(
        grant_id=grant_id,
        turn_id=turn_id,
        worker_session_id=worker_session_id,
        attempt_no=attempt_no,
        access_mode=access_mode,
        host_snapshot_revision=host_snapshot.revision,
        host_snapshot_digest=str(host_snapshot.source_set_digest),
        explorer_high_water_seq=explorer_high_water_seq,
        published_documents=published_documents,
        portfolio=portfolio,
    )


class ExplorerAttemptAccessAPI:
    """Broker-facing API bound to exactly one immutable attempt grant."""

    def __init__(
        self,
        grant: ExplorerAttemptAccessGrant,
        *,
        audit: ExplorerAccessAuditPort | None = None,
    ) -> None:
        self.grant = grant
        self.audit = audit or _NullAudit()

    def check_result(self, kind: str, statement: str) -> list[dict[str, Any]]:
        if self.grant.access_mode not in {"check-result", "portfolio"}:
            raise ExplorerAccessError(
                "check-result is unavailable under this attempt access grant"
            )
        kind, statement = validate_check_result_request(kind, statement)
        candidates = sorted(
            (
                item
                for item in self.grant.published_documents
                if item.memory_kind in {"fact", "claim", "computation"}
            ),
            key=lambda item: (item.memory_kind, item.source_id),
        )
        ranked = _bm25_scores(
            statement,
            [
                (
                    f"{item.memory_kind}:{item.source_id}",
                    f"{item.abstract}\n{item.main_content}",
                )
                for item in candidates
            ],
        )
        # A zero BM25 score is not a sufficiently close result.  There is no
        # pagination or caller-controlled limit; every call returns at most 3.
        selected = [entry for entry in ranked if entry[1] > 0.0][:3]
        result: list[dict[str, Any]] = []
        private_matches: list[dict[str, Any]] = []
        for index, score in selected:
            document = candidates[index]
            opaque_id = _identity(
                "XCR-", self.grant.grant_id, document.source_digest
            )
            result.append(
                {
                    "result_id": opaque_id,
                    "status": "established",
                    "abstract": document.abstract,
                    "main_content": document.main_content,
                    "relevance": round(score, 8),
                }
            )
            private_matches.append(
                {
                    "result_id": opaque_id,
                    "source_id": document.source_id,
                    "memory_kind": document.memory_kind,
                    "source_digest": document.source_digest,
                    "relevance": round(score, 8),
                }
            )
        self.audit.append(
            {
                "action": "check_result",
                "grant_id": self.grant.grant_id,
                "turn_id": self.grant.turn_id,
                "worker_session_id": self.grant.worker_session_id,
                "attempt_no": self.grant.attempt_no,
                "kind": kind,
                "statement": statement,
                "private_matches": private_matches,
            }
        )
        return result

    def portfolio_search(
        self, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        portfolio = self._portfolio()
        if (
            not isinstance(query, str)
            or not query
            or query != query.strip()
            or len(query.encode("utf-8")) > 8_192
        ):
            raise ExplorerAccessError("portfolio-search query must be exact text")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ExplorerAccessError("portfolio-search limit must be between 1 and 10")
        ranked = _bm25_scores(
            query,
            [
                (
                    item.portfolio_item_id,
                    f"{item.abstract}\n{item.main_content}",
                )
                for item in portfolio.items
            ],
        )
        selected = [entry for entry in ranked if entry[1] > 0.0][:limit]
        result = [
            {
                **portfolio.items[index].agent_view(),
                "relevance": round(score, 8),
            }
            for index, score in selected
        ]
        self.audit.append(
            {
                "action": "portfolio_search",
                "grant_id": self.grant.grant_id,
                "portfolio_id": portfolio.portfolio_id,
                "query": query,
                "limit": limit,
                "private_source_ids": [
                    portfolio.items[index].source_id for index, _ in selected
                ],
            }
        )
        return result

    def portfolio_fetch(self, portfolio_item_id: str) -> dict[str, str]:
        portfolio = self._portfolio()
        if not isinstance(portfolio_item_id, str):
            raise ExplorerAccessError("portfolio item ID must be a string")
        item = next(
            (
                candidate
                for candidate in portfolio.items
                if candidate.portfolio_item_id == portfolio_item_id
            ),
            None,
        )
        if item is None:
            raise ExplorerAccessError("portfolio item is outside this frozen grant")
        self.audit.append(
            {
                "action": "portfolio_fetch",
                "grant_id": self.grant.grant_id,
                "portfolio_id": portfolio.portfolio_id,
                "portfolio_item_id": portfolio_item_id,
                "private_source_id": item.source_id,
                "private_source_kind": item.item_kind,
            }
        )
        return item.agent_view()

    def _portfolio(self) -> ExplorerPortfolioSnapshot:
        if self.grant.access_mode != "portfolio" or self.grant.portfolio is None:
            raise ExplorerAccessError(
                "portfolio access is unavailable under this attempt grant"
            )
        return self.grant.portfolio


__all__ = [
    "CHECK_RESULT_KINDS",
    "ExplorerAccessAuditPort",
    "ExplorerAttemptAccessAPI",
    "build_attempt_access_grant",
    "validate_check_result_request",
]
