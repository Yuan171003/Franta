"""Immutable value contracts owned by the portable Advisor subsystem.

The host authenticates memory snapshots, persists artifacts, and executes model
calls.  These contracts make the mathematical choice and every binding between
the two Advisor calls explicit and independently checkable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Literal, Mapping, Sequence


OBLIGATION_COUNT = 5
MAX_SELECTED_SUBPROBLEMS = 2
BREAKTHROUGH_FRESHNESS_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROBLEM_ASSIGNMENT_PREAMBLE = (
    "Solve the Original Problem. For this research cycle, you *should* attack "
    "the Original Problem by first solving the Subproblem(s), and treat the "
    "Subproblem(s) also as the *primary objective*. You may switch to another "
    "route *only after* recording concrete evidence that the assigned "
    "Subproblem(s) is materially less promising, and that a named alternative "
    "is substantially more likely to produce decisive progress toward the "
    "Original Problem."
)
_LEGACY_PROBLEM_ASSIGNMENT_PREAMBLE = (
    "The task is to solve the original problem, and you are encouraged to "
    "attack it by solving the subproblems."
)


class AdvisorContractError(ValueError):
    """Raised when an Advisor artifact violates a portable invariant."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    """Return the subsystem's stable JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_value(value: Any) -> str:
    """Hash one JSON-compatible value using the stable representation."""

    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdvisorContractError(f"{name} must be a non-empty string", code=f"invalid_{name}")
    if value != value.strip():
        raise AdvisorContractError(
            f"{name} must not have leading or trailing whitespace",
            code=f"noncanonical_{name}",
        )
    return value


def _identifier(value: object, name: str) -> str:
    value = _text(value, name)
    if any(character.isspace() for character in value):
        raise AdvisorContractError(
            f"{name} must not contain whitespace", code=f"invalid_{name}"
        )
    return value


def _digest(value: object, name: str) -> str:
    value = _text(value, name)
    if not _SHA256_RE.fullmatch(value):
        raise AdvisorContractError(
            f"{name} must be a lowercase SHA-256 digest", code=f"invalid_{name}"
        )
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AdvisorContractError(
            f"{name} must be a positive integer", code=f"invalid_{name}"
        )
    return value


def _string_tuple(values: Iterable[object], name: str) -> tuple[str, ...]:
    result = tuple(_identifier(value, f"{name}_item") for value in values)
    if len(result) != len(set(result)):
        raise AdvisorContractError(f"{name} must be unique", code=f"duplicate_{name}")
    return result


@dataclass(frozen=True)
class AdvisorMemorySnapshot:
    """Host-authenticated, immutable view of the preceding Franta memories."""

    snapshot_id: str
    snapshot_digest: str
    source_event_cursor: int
    relative_path: str

    def __post_init__(self) -> None:
        _identifier(self.snapshot_id, "snapshot_id")
        _digest(self.snapshot_digest, "snapshot_digest")
        if (
            not isinstance(self.source_event_cursor, int)
            or isinstance(self.source_event_cursor, bool)
            or self.source_event_cursor < 0
        ):
            raise AdvisorContractError(
                "source_event_cursor must be a non-negative integer",
                code="invalid_source_event_cursor",
            )
        _text(self.relative_path, "relative_path")
        if self.relative_path.startswith("/") or ".." in self.relative_path.split("/"):
            raise AdvisorContractError(
                "relative_path must be a confined relative path",
                code="unsafe_relative_path",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "source_event_cursor": self.source_event_cursor,
            "relative_path": self.relative_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdvisorMemorySnapshot":
        return cls(
            snapshot_id=value["snapshot_id"],
            snapshot_digest=value["snapshot_digest"],
            source_event_cursor=value["source_event_cursor"],
            relative_path=value["relative_path"],
        )


@dataclass(frozen=True)
class SelectedSubproblem:
    """One subproblem selected strictly from a human feedback choice."""

    source: Literal["listed", "human_override"]
    statement: str
    human_choice_index: int
    obligation_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in ("listed", "human_override"):
            raise AdvisorContractError("invalid subproblem source", code="invalid_source")
        _text(self.statement, "statement")
        if self.human_choice_index not in (1, 2):
            raise AdvisorContractError(
                "human_choice_index must be 1 or 2", code="invalid_human_choice_index"
            )
        if self.source == "listed":
            _identifier(self.obligation_id, "obligation_id")
        elif self.obligation_id is not None:
            raise AdvisorContractError(
                "human overrides cannot claim a listed obligation_id",
                code="unexpected_obligation_id",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "obligation_id": self.obligation_id,
            "statement": self.statement,
            "human_choice_index": self.human_choice_index,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SelectedSubproblem":
        return cls(
            source=value["source"],
            obligation_id=value.get("obligation_id"),
            statement=value["statement"],
            human_choice_index=value["human_choice_index"],
        )


def _render_problem_assignment_with_preamble(
    original_problem: str,
    subproblems: Sequence[SelectedSubproblem],
    preamble: str,
) -> str:
    _text(original_problem, "original_problem")
    if not 1 <= len(subproblems) <= MAX_SELECTED_SUBPROBLEMS:
        raise AdvisorContractError(
            "a problem assignment requires one or two subproblems",
            code="invalid_subproblem_count",
        )
    sections = [
        preamble,
        f"Original Problem:\n{original_problem}",
    ]
    sections.extend(
        f"Subproblem {index}:\n{subproblem.statement}"
        for index, subproblem in enumerate(subproblems, start=1)
    )
    return "\n\n".join(sections) + "\n"


def render_problem_assignment(
    original_problem: str, subproblems: Sequence[SelectedSubproblem]
) -> str:
    """Render the exact visible problem handed to the next research pair."""

    return _render_problem_assignment_with_preamble(
        original_problem,
        subproblems,
        _PROBLEM_ASSIGNMENT_PREAMBLE,
    )


@dataclass(frozen=True)
class ProblemAssignment:
    """Immutable visible ROOT problem for exactly one later research cycle."""

    assignment_id: str
    advisor_index: int
    source_cycle: int
    target_cycle: int
    original_problem: str
    selection_report_id: str
    selection_report_digest: str
    feedback_id: str
    feedback_digest: str
    subproblems: tuple[SelectedSubproblem, ...]
    problem_text: str | None = field(default=None)

    def __post_init__(self) -> None:
        _identifier(self.assignment_id, "assignment_id")
        _positive_int(self.advisor_index, "advisor_index")
        _positive_int(self.source_cycle, "source_cycle")
        _positive_int(self.target_cycle, "target_cycle")
        if self.source_cycle != self.advisor_index or self.target_cycle != self.source_cycle + 1:
            raise AdvisorContractError(
                "assignment cycles must map Advisor i to research cycle i+1",
                code="invalid_assignment_cycle",
            )
        _text(self.original_problem, "original_problem")
        _identifier(self.selection_report_id, "selection_report_id")
        _digest(self.selection_report_digest, "selection_report_digest")
        _identifier(self.feedback_id, "feedback_id")
        _digest(self.feedback_digest, "feedback_digest")
        if not isinstance(self.subproblems, tuple):
            object.__setattr__(self, "subproblems", tuple(self.subproblems))
        expected = render_problem_assignment(self.original_problem, self.subproblems)
        if self.problem_text is None:
            object.__setattr__(self, "problem_text", expected)
        elif self.problem_text not in {
            expected,
            _render_problem_assignment_with_preamble(
                self.original_problem,
                self.subproblems,
                _LEGACY_PROBLEM_ASSIGNMENT_PREAMBLE,
            ),
        }:
            raise AdvisorContractError(
                "problem_text must equal the deterministic rendering",
                code="problem_text_mismatch",
            )

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "advisor_index": self.advisor_index,
            "source_cycle": self.source_cycle,
            "target_cycle": self.target_cycle,
            "original_problem": self.original_problem,
            "selection_report_id": self.selection_report_id,
            "selection_report_digest": self.selection_report_digest,
            "feedback_id": self.feedback_id,
            "feedback_digest": self.feedback_digest,
            "subproblems": [item.to_dict() for item in self.subproblems],
            "problem_text": self.problem_text,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProblemAssignment":
        return cls(
            assignment_id=value["assignment_id"],
            advisor_index=value["advisor_index"],
            source_cycle=value["source_cycle"],
            target_cycle=value["target_cycle"],
            original_problem=value["original_problem"],
            selection_report_id=value["selection_report_id"],
            selection_report_digest=value["selection_report_digest"],
            feedback_id=value["feedback_id"],
            feedback_digest=value["feedback_digest"],
            subproblems=tuple(
                SelectedSubproblem.from_dict(item) for item in value["subproblems"]
            ),
            problem_text=value.get("problem_text"),
        )


@dataclass(frozen=True)
class AdvisorCycleContext:
    """All immutable inputs bound before an Advisor proposal call starts."""

    advisor_index: int
    source_cycle: int
    target_cycle: int
    original_problem: str
    memory_snapshot: AdvisorMemorySnapshot
    previous_assignments: tuple[ProblemAssignment, ...] = ()

    def __post_init__(self) -> None:
        _positive_int(self.advisor_index, "advisor_index")
        _positive_int(self.source_cycle, "source_cycle")
        _positive_int(self.target_cycle, "target_cycle")
        if self.source_cycle != self.advisor_index or self.target_cycle != self.source_cycle + 1:
            raise AdvisorContractError(
                "context cycles must map Advisor i to research cycle i+1",
                code="invalid_context_cycle",
            )
        _text(self.original_problem, "original_problem")
        if not isinstance(self.previous_assignments, tuple):
            object.__setattr__(self, "previous_assignments", tuple(self.previous_assignments))
        ids: list[str] = []
        for assignment in self.previous_assignments:
            if assignment.advisor_index >= self.advisor_index:
                raise AdvisorContractError(
                    "previous assignments must precede this Advisor index",
                    code="future_previous_assignment",
                )
            if assignment.original_problem != self.original_problem:
                raise AdvisorContractError(
                    "previous assignments must bind the same original problem",
                    code="original_problem_changed",
                )
            ids.append(assignment.assignment_id)
        if len(ids) != len(set(ids)):
            raise AdvisorContractError(
                "previous assignment IDs must be unique",
                code="duplicate_previous_assignment",
            )

    @property
    def original_problem_digest(self) -> str:
        return digest_value(self.original_problem)

    @property
    def previous_assignments_digest(self) -> str:
        return digest_value([item.to_dict() for item in self.previous_assignments])

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisor_index": self.advisor_index,
            "source_cycle": self.source_cycle,
            "target_cycle": self.target_cycle,
            "original_problem": self.original_problem,
            "original_problem_digest": self.original_problem_digest,
            "memory_snapshot": self.memory_snapshot.to_dict(),
            "previous_assignments": [item.to_dict() for item in self.previous_assignments],
            "previous_assignments_digest": self.previous_assignments_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdvisorCycleContext":
        context = cls(
            advisor_index=value["advisor_index"],
            source_cycle=value["source_cycle"],
            target_cycle=value["target_cycle"],
            original_problem=value["original_problem"],
            memory_snapshot=AdvisorMemorySnapshot.from_dict(value["memory_snapshot"]),
            previous_assignments=tuple(
                ProblemAssignment.from_dict(item)
                for item in value.get("previous_assignments", ())
            ),
        )
        if value.get("original_problem_digest", context.original_problem_digest) != context.original_problem_digest:
            raise AdvisorContractError(
                "original_problem_digest does not match the context",
                code="original_problem_digest_mismatch",
            )
        if value.get("previous_assignments_digest", context.previous_assignments_digest) != context.previous_assignments_digest:
            raise AdvisorContractError(
                "previous_assignments_digest does not match the context",
                code="previous_assignments_digest_mismatch",
            )
        return context


@dataclass(frozen=True)
class RankedObligation:
    """A substantial research obligation proposed for the next cycle."""

    obligation_id: str
    rank: int
    title: str
    statement: str
    importance: str
    landscape_change: str
    relationship_to_root: str
    novelty: str
    previous_assignment_ids: tuple[str, ...] = ()
    repeat_justification: str | None = None
    breakthrough_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.obligation_id, "obligation_id")
        if self.rank not in range(1, OBLIGATION_COUNT + 1):
            raise AdvisorContractError("rank must be between 1 and 5", code="invalid_rank")
        for name in (
            "title",
            "statement",
            "importance",
            "landscape_change",
            "relationship_to_root",
            "novelty",
        ):
            _text(getattr(self, name), name)
        previous = _string_tuple(self.previous_assignment_ids, "previous_assignment_ids")
        evidence = _string_tuple(self.breakthrough_evidence_ids, "breakthrough_evidence_ids")
        object.__setattr__(self, "previous_assignment_ids", previous)
        object.__setattr__(self, "breakthrough_evidence_ids", evidence)
        if previous:
            _text(self.repeat_justification, "repeat_justification")
            if not evidence:
                raise AdvisorContractError(
                    "a repeated obligation requires breakthrough evidence IDs",
                    code="missing_breakthrough_evidence",
                )
        elif self.repeat_justification is not None:
            raise AdvisorContractError(
                "repeat_justification is only valid for a repeated obligation",
                code="unexpected_repeat_justification",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "rank": self.rank,
            "title": self.title,
            "statement": self.statement,
            "importance": self.importance,
            "landscape_change": self.landscape_change,
            "relationship_to_root": self.relationship_to_root,
            "novelty": self.novelty,
            "previous_assignment_ids": list(self.previous_assignment_ids),
            "repeat_justification": self.repeat_justification,
            "breakthrough_evidence_ids": list(self.breakthrough_evidence_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RankedObligation":
        return cls(
            obligation_id=value["obligation_id"],
            rank=value["rank"],
            title=value["title"],
            statement=value["statement"],
            importance=value["importance"],
            landscape_change=value["landscape_change"],
            relationship_to_root=value["relationship_to_root"],
            novelty=value["novelty"],
            previous_assignment_ids=tuple(value.get("previous_assignment_ids", ())),
            repeat_justification=value.get("repeat_justification"),
            breakthrough_evidence_ids=tuple(value.get("breakthrough_evidence_ids", ())),
        )


@dataclass(frozen=True)
class SelectionReport:
    """The five-option, human-readable output of an Advisor proposal call."""

    selection_report_id: str
    feedback_request_id: str
    advisor_index: int
    source_cycle: int
    target_cycle: int
    original_problem_digest: str
    memory_snapshot_id: str
    memory_snapshot_digest: str
    previous_assignments_digest: str
    obligations: tuple[RankedObligation, ...]
    human_question: str
    report_path: str = "artifacts/selection-report.md"

    def __post_init__(self) -> None:
        _identifier(self.selection_report_id, "selection_report_id")
        _identifier(self.feedback_request_id, "feedback_request_id")
        _positive_int(self.advisor_index, "advisor_index")
        _positive_int(self.source_cycle, "source_cycle")
        _positive_int(self.target_cycle, "target_cycle")
        _digest(self.original_problem_digest, "original_problem_digest")
        _identifier(self.memory_snapshot_id, "memory_snapshot_id")
        _digest(self.memory_snapshot_digest, "memory_snapshot_digest")
        _digest(self.previous_assignments_digest, "previous_assignments_digest")
        if not isinstance(self.obligations, tuple):
            object.__setattr__(self, "obligations", tuple(self.obligations))
        if len(self.obligations) != OBLIGATION_COUNT:
            raise AdvisorContractError(
                "a selection report must contain exactly five obligations",
                code="invalid_obligation_count",
            )
        if tuple(item.rank for item in self.obligations) != tuple(range(1, OBLIGATION_COUNT + 1)):
            raise AdvisorContractError(
                "obligations must be stored in exact rank order 1 through 5",
                code="invalid_obligation_ranking",
            )
        ids = [item.obligation_id for item in self.obligations]
        statements = [item.statement for item in self.obligations]
        if len(ids) != len(set(ids)):
            raise AdvisorContractError("obligation IDs must be unique", code="duplicate_obligation_id")
        if len(statements) != len(set(statements)):
            raise AdvisorContractError(
                "obligation statements must be distinct", code="duplicate_obligation_statement"
            )
        _text(self.human_question, "human_question")
        _text(self.report_path, "report_path")
        if self.report_path.startswith("/") or ".." in self.report_path.split("/"):
            raise AdvisorContractError(
                "report_path must be a confined relative path",
                code="unsafe_report_path",
            )

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def validate_for_context(self, context: AdvisorCycleContext) -> None:
        expected = (
            context.advisor_index,
            context.source_cycle,
            context.target_cycle,
            context.original_problem_digest,
            context.memory_snapshot.snapshot_id,
            context.memory_snapshot.snapshot_digest,
            context.previous_assignments_digest,
        )
        actual = (
            self.advisor_index,
            self.source_cycle,
            self.target_cycle,
            self.original_problem_digest,
            self.memory_snapshot_id,
            self.memory_snapshot_digest,
            self.previous_assignments_digest,
        )
        if actual != expected:
            raise AdvisorContractError(
                "selection report is not bound to the active context",
                code="selection_report_context_mismatch",
            )
        previous_by_id = {item.assignment_id: item for item in context.previous_assignments}
        prior_statements = {
            item.statement: assignment.assignment_id
            for assignment in context.previous_assignments
            for item in assignment.subproblems
        }
        for obligation in self.obligations:
            if obligation.statement == context.original_problem:
                raise AdvisorContractError(
                    "an obligation must be different from the original problem",
                    code="obligation_duplicates_root",
                )
            unknown = set(obligation.previous_assignment_ids).difference(previous_by_id)
            if unknown:
                raise AdvisorContractError(
                    "repeated obligation references an unknown previous assignment",
                    code="unknown_previous_assignment",
                )
            prior_assignment_id = prior_statements.get(obligation.statement)
            if prior_assignment_id is not None and prior_assignment_id not in obligation.previous_assignment_ids:
                raise AdvisorContractError(
                    "an exactly repeated subproblem must declare its previous assignment",
                    code="undeclared_repeated_obligation",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_report_id": self.selection_report_id,
            "feedback_request_id": self.feedback_request_id,
            "advisor_index": self.advisor_index,
            "source_cycle": self.source_cycle,
            "target_cycle": self.target_cycle,
            "original_problem_digest": self.original_problem_digest,
            "memory_snapshot_id": self.memory_snapshot_id,
            "memory_snapshot_digest": self.memory_snapshot_digest,
            "previous_assignments_digest": self.previous_assignments_digest,
            "obligations": [item.to_dict() for item in self.obligations],
            "human_question": self.human_question,
            "report_path": self.report_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SelectionReport":
        return cls(
            selection_report_id=value["selection_report_id"],
            feedback_request_id=value["feedback_request_id"],
            advisor_index=value["advisor_index"],
            source_cycle=value["source_cycle"],
            target_cycle=value["target_cycle"],
            original_problem_digest=value["original_problem_digest"],
            memory_snapshot_id=value["memory_snapshot_id"],
            memory_snapshot_digest=value["memory_snapshot_digest"],
            previous_assignments_digest=value["previous_assignments_digest"],
            obligations=tuple(RankedObligation.from_dict(item) for item in value["obligations"]),
            human_question=value["human_question"],
            report_path=value.get("report_path", "artifacts/selection-report.md"),
        )


def _memory_revision_map(value: Mapping[str, Any], name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise AdvisorContractError(
            f"{name} must be an object", code="invalid_memory_revision_map"
        )
    result: dict[str, int] = {}
    for memory_id, revision in value.items():
        identifier = _identifier(memory_id, f"{name}_memory_id")
        result[identifier] = _positive_int(revision, f"{name}_{identifier}_revision")
    return result


def build_breakthrough_evidence_freshness(
    context: AdvisorCycleContext,
    *,
    current_revisions: Mapping[str, Any],
    previous_revisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compress historical snapshot revisions into a proposal-time proof.

    A record's ``newer_than_advisor_index`` is the latest prior Advisor index
    whose bound memory snapshot either did not contain the record or contained
    a lower revision. This checks freshness against any referenced assignment
    without copying every historical catalog into the next agent workspace.
    """

    current = _memory_revision_map(current_revisions, "current_revisions")
    expected_ids = {item.assignment_id for item in context.previous_assignments}
    if set(previous_revisions) != expected_ids:
        raise AdvisorContractError(
            "historical revision catalogs do not match previous assignments",
            code="previous_revision_history_mismatch",
        )
    baselines = {
        assignment_id: _memory_revision_map(
            previous_revisions[assignment_id],
            f"previous_revisions_{assignment_id}",
        )
        for assignment_id in expected_ids
    }
    records: dict[str, dict[str, int]] = {}
    for memory_id, revision in current.items():
        newer_than = 0
        for assignment in context.previous_assignments:
            prior_revision = baselines[assignment.assignment_id].get(memory_id)
            if prior_revision is None or prior_revision < revision:
                newer_than = max(newer_than, assignment.advisor_index)
            elif prior_revision > revision:
                raise AdvisorContractError(
                    f"memory {memory_id} regressed below a historical revision",
                    code="memory_revision_regressed",
                )
        if newer_than:
            records[memory_id] = {
                "revision": revision,
                "newer_than_advisor_index": newer_than,
            }
    return {
        "schema_version": BREAKTHROUGH_FRESHNESS_SCHEMA_VERSION,
        "memory_snapshot_id": context.memory_snapshot.snapshot_id,
        "memory_snapshot_digest": context.memory_snapshot.snapshot_digest,
        "records": records,
    }


def validate_breakthrough_evidence_freshness(
    report: SelectionReport,
    context: AdvisorCycleContext,
    *,
    current_revisions: Mapping[str, Any],
    freshness: Mapping[str, Any],
) -> None:
    """Require cited repeat evidence to postdate every referenced assignment."""

    report.validate_for_context(context)
    current = _memory_revision_map(current_revisions, "current_revisions")
    if not isinstance(freshness, Mapping) or set(freshness) != {
        "schema_version",
        "memory_snapshot_id",
        "memory_snapshot_digest",
        "records",
    }:
        raise AdvisorContractError(
            "breakthrough freshness descriptor has an invalid shape",
            code="invalid_breakthrough_freshness",
        )
    if (
        freshness.get("schema_version") != BREAKTHROUGH_FRESHNESS_SCHEMA_VERSION
        or freshness.get("memory_snapshot_id") != context.memory_snapshot.snapshot_id
        or freshness.get("memory_snapshot_digest")
        != context.memory_snapshot.snapshot_digest
    ):
        raise AdvisorContractError(
            "breakthrough freshness is not bound to the active memory snapshot",
            code="breakthrough_freshness_context_mismatch",
        )
    raw_records = freshness.get("records")
    if not isinstance(raw_records, Mapping):
        raise AdvisorContractError(
            "breakthrough freshness records must be an object",
            code="invalid_breakthrough_freshness",
        )
    records: dict[str, tuple[int, int]] = {}
    for memory_id, raw_entry in raw_records.items():
        identifier = _identifier(memory_id, "breakthrough_freshness_memory_id")
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "revision",
            "newer_than_advisor_index",
        }:
            raise AdvisorContractError(
                f"breakthrough freshness entry for {identifier} is malformed",
                code="invalid_breakthrough_freshness",
            )
        revision = _positive_int(
            raw_entry.get("revision"),
            f"breakthrough_freshness_{identifier}_revision",
        )
        newer_than = _positive_int(
            raw_entry.get("newer_than_advisor_index"),
            f"breakthrough_freshness_{identifier}_advisor_index",
        )
        if current.get(identifier) != revision or newer_than >= context.advisor_index:
            raise AdvisorContractError(
                f"breakthrough freshness entry for {identifier} is inconsistent",
                code="invalid_breakthrough_freshness",
            )
        records[identifier] = (revision, newer_than)

    assignments = {
        item.assignment_id: item for item in context.previous_assignments
    }
    for obligation in report.obligations:
        for evidence_id in obligation.breakthrough_evidence_ids:
            entry = records.get(evidence_id)
            if entry is None:
                raise AdvisorContractError(
                    f"breakthrough evidence {evidence_id} is not newer than the "
                    "referenced assignment",
                    code="stale_breakthrough_evidence",
                )
            _revision, newer_than = entry
            stale_for = [
                assignment_id
                for assignment_id in obligation.previous_assignment_ids
                if assignments[assignment_id].advisor_index > newer_than
            ]
            if stale_for:
                raise AdvisorContractError(
                    f"breakthrough evidence {evidence_id} is not newer than "
                    + ", ".join(stale_for),
                    code="stale_breakthrough_evidence",
                )


def render_selection_report_markdown(report: SelectionReport) -> str:
    """Render the trusted five-obligation artifact for a human reader."""

    lines = [
        "# Advisor selection report",
        "",
        (
            f"Advisor {report.advisor_index} proposes the following obligations for "
            f"research cycle {report.target_cycle}, ranked from most to least worth "
            "trying next."
        ),
        "",
    ]
    for obligation in report.obligations:
        lines.extend(
            [
                f"## {obligation.rank}. {obligation.title}",
                "",
                f"**Obligation.** {obligation.statement}",
                "",
                f"**Independent importance.** {obligation.importance}",
                "",
                f"**Landscape change.** {obligation.landscape_change}",
                "",
                f"**Relation to the original problem.** {obligation.relationship_to_root}",
                "",
                f"**Novelty relative to prior assignments.** {obligation.novelty}",
                "",
            ]
        )
        if obligation.previous_assignment_ids:
            lines.extend(
                [
                    "**Exceptional repeat.** "
                    f"Prior assignments: {', '.join(obligation.previous_assignment_ids)}. "
                    f"{obligation.repeat_justification}",
                    "",
                    "**Breakthrough evidence.** "
                    f"{', '.join(obligation.breakthrough_evidence_ids)}",
                    "",
                ]
            )
    lines.extend(
        [
            "## Human decision requested",
            "",
            report.human_question,
            "",
            (
                f"Report ID: `{report.selection_report_id}`  \n"
                f"Feedback request ID: `{report.feedback_request_id}`"
            ),
            "",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class FeedbackChoice:
    """One binding human selection, including an unlisted override."""

    kind: Literal["listed", "custom"]
    obligation_id: str | None = None
    statement: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "listed":
            _identifier(self.obligation_id, "obligation_id")
            if self.statement is not None:
                raise AdvisorContractError(
                    "listed feedback must not restate the obligation",
                    code="unexpected_feedback_statement",
                )
        elif self.kind == "custom":
            _text(self.statement, "statement")
            if self.obligation_id is not None:
                raise AdvisorContractError(
                    "custom feedback must not name a listed obligation",
                    code="unexpected_feedback_obligation_id",
                )
        else:
            raise AdvisorContractError("invalid feedback choice kind", code="invalid_choice_kind")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "obligation_id": self.obligation_id,
            "statement": self.statement,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FeedbackChoice":
        allowed = {"kind", "obligation_id", "statement"}
        unknown = set(value).difference(allowed)
        if unknown:
            raise AdvisorContractError(
                "feedback choice has unknown fields: "
                + ", ".join(sorted(str(item) for item in unknown)),
                code="unknown_feedback_choice_field",
            )
        return cls(
            kind=value["kind"],
            obligation_id=value.get("obligation_id"),
            statement=value.get("statement"),
        )


@dataclass(frozen=True)
class HumanFeedback:
    """Structured human authority for the Advisor's second call."""

    feedback_id: str
    feedback_request_id: str
    selection_report_id: str
    selection_report_digest: str
    choices: tuple[FeedbackChoice, ...]
    instructions: str

    def __post_init__(self) -> None:
        _identifier(self.feedback_id, "feedback_id")
        _identifier(self.feedback_request_id, "feedback_request_id")
        _identifier(self.selection_report_id, "selection_report_id")
        _digest(self.selection_report_digest, "selection_report_digest")
        if not isinstance(self.choices, tuple):
            object.__setattr__(self, "choices", tuple(self.choices))
        if not 1 <= len(self.choices) <= MAX_SELECTED_SUBPROBLEMS:
            raise AdvisorContractError(
                "human feedback must choose one or two subproblems",
                code="invalid_feedback_choice_count",
            )
        choice_keys = [
            (item.kind, item.obligation_id if item.kind == "listed" else item.statement)
            for item in self.choices
        ]
        if len(choice_keys) != len(set(choice_keys)):
            raise AdvisorContractError(
                "human feedback choices must be distinct", code="duplicate_feedback_choice"
            )
        _text(self.instructions, "instructions")

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def validate_for_report(self, report: SelectionReport) -> None:
        if (
            self.feedback_request_id != report.feedback_request_id
            or self.selection_report_id != report.selection_report_id
            or self.selection_report_digest != report.digest
        ):
            raise AdvisorContractError(
                "human feedback is not bound to the selection report",
                code="feedback_report_mismatch",
            )
        obligation_ids = {item.obligation_id for item in report.obligations}
        for choice in self.choices:
            if choice.kind == "listed" and choice.obligation_id not in obligation_ids:
                raise AdvisorContractError(
                    "human feedback names an unlisted obligation as listed",
                    code="unknown_feedback_obligation",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "feedback_request_id": self.feedback_request_id,
            "selection_report_id": self.selection_report_id,
            "selection_report_digest": self.selection_report_digest,
            "choices": [item.to_dict() for item in self.choices],
            "instructions": self.instructions,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HumanFeedback":
        expected = {
            "feedback_id",
            "feedback_request_id",
            "selection_report_id",
            "selection_report_digest",
            "choices",
            "instructions",
        }
        if set(value) != expected:
            raise AdvisorContractError(
                "human feedback has an invalid field set",
                code="invalid_human_feedback_fields",
            )
        return cls(
            feedback_id=value["feedback_id"],
            feedback_request_id=value["feedback_request_id"],
            selection_report_id=value["selection_report_id"],
            selection_report_digest=value["selection_report_digest"],
            choices=tuple(FeedbackChoice.from_dict(item) for item in value["choices"]),
            instructions=value["instructions"],
        )


@dataclass(frozen=True)
class AdvisorFinalization:
    """Structured second-call claim, checked against binding human feedback."""

    selection_report_id: str
    selection_report_digest: str
    feedback_id: str
    feedback_digest: str
    selected_subproblems: tuple[SelectedSubproblem, ...]

    def __post_init__(self) -> None:
        _identifier(self.selection_report_id, "selection_report_id")
        _digest(self.selection_report_digest, "selection_report_digest")
        _identifier(self.feedback_id, "feedback_id")
        _digest(self.feedback_digest, "feedback_digest")
        if not isinstance(self.selected_subproblems, tuple):
            object.__setattr__(self, "selected_subproblems", tuple(self.selected_subproblems))
        if not 1 <= len(self.selected_subproblems) <= MAX_SELECTED_SUBPROBLEMS:
            raise AdvisorContractError(
                "finalization must contain one or two subproblems",
                code="invalid_finalization_count",
            )

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict())

    def validate_bindings(self, report: SelectionReport, feedback: HumanFeedback) -> None:
        feedback.validate_for_report(report)
        if (
            self.selection_report_id != report.selection_report_id
            or self.selection_report_digest != report.digest
            or self.feedback_id != feedback.feedback_id
            or self.feedback_digest != feedback.digest
        ):
            raise AdvisorContractError(
                "finalization is not bound to the report and feedback",
                code="finalization_binding_mismatch",
            )
        if len(self.selected_subproblems) != len(feedback.choices):
            raise AdvisorContractError(
                "finalization must preserve every human choice",
                code="finalization_choice_count_mismatch",
            )
        obligations = {item.obligation_id: item for item in report.obligations}
        for index, (selected, choice) in enumerate(
            zip(self.selected_subproblems, feedback.choices, strict=True), start=1
        ):
            if selected.human_choice_index != index:
                raise AdvisorContractError(
                    "finalization must preserve human choice order",
                    code="finalization_choice_order_mismatch",
                )
            if choice.kind == "listed":
                expected = obligations[choice.obligation_id]
                if (
                    selected.source != "listed"
                    or selected.obligation_id != choice.obligation_id
                    or selected.statement != expected.statement
                ):
                    raise AdvisorContractError(
                        "listed feedback was not followed exactly",
                        code="listed_feedback_not_followed",
                    )
            elif (
                selected.source != "human_override"
                or selected.obligation_id is not None
                or selected.statement != choice.statement
            ):
                raise AdvisorContractError(
                    "custom human feedback was not followed exactly",
                    code="custom_feedback_not_followed",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_report_id": self.selection_report_id,
            "selection_report_digest": self.selection_report_digest,
            "feedback_id": self.feedback_id,
            "feedback_digest": self.feedback_digest,
            "selected_subproblems": [item.to_dict() for item in self.selected_subproblems],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdvisorFinalization":
        return cls(
            selection_report_id=value["selection_report_id"],
            selection_report_digest=value["selection_report_digest"],
            feedback_id=value["feedback_id"],
            feedback_digest=value["feedback_digest"],
            selected_subproblems=tuple(
                SelectedSubproblem.from_dict(item)
                for item in value["selected_subproblems"]
            ),
        )


def build_problem_assignment(
    *,
    assignment_id: str,
    context: AdvisorCycleContext,
    report: SelectionReport,
    feedback: HumanFeedback,
    finalization: AdvisorFinalization,
) -> ProblemAssignment:
    """Validate every authority edge and construct the immutable assignment."""

    report.validate_for_context(context)
    finalization.validate_bindings(report, feedback)
    return ProblemAssignment(
        assignment_id=assignment_id,
        advisor_index=context.advisor_index,
        source_cycle=context.source_cycle,
        target_cycle=context.target_cycle,
        original_problem=context.original_problem,
        selection_report_id=report.selection_report_id,
        selection_report_digest=report.digest,
        feedback_id=feedback.feedback_id,
        feedback_digest=feedback.digest,
        subproblems=finalization.selected_subproblems,
    )


__all__ = [
    "BREAKTHROUGH_FRESHNESS_SCHEMA_VERSION",
    "AdvisorContractError",
    "AdvisorCycleContext",
    "AdvisorFinalization",
    "AdvisorMemorySnapshot",
    "FeedbackChoice",
    "HumanFeedback",
    "MAX_SELECTED_SUBPROBLEMS",
    "OBLIGATION_COUNT",
    "ProblemAssignment",
    "RankedObligation",
    "SelectedSubproblem",
    "SelectionReport",
    "build_breakthrough_evidence_freshness",
    "build_problem_assignment",
    "canonical_json",
    "digest_value",
    "render_problem_assignment",
    "render_selection_report_markdown",
    "validate_breakthrough_evidence_freshness",
]
