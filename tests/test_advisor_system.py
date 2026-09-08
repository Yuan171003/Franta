from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from advisor_system.contracts import (
    AdvisorContractError,
    AdvisorCycleContext,
    AdvisorFinalization,
    AdvisorMemorySnapshot,
    FeedbackChoice,
    HumanFeedback,
    ProblemAssignment,
    RankedObligation,
    SelectedSubproblem,
    SelectionReport,
    build_breakthrough_evidence_freshness,
    build_problem_assignment,
    render_problem_assignment,
    validate_breakthrough_evidence_freshness,
)
from advisor_system.state import (
    STATUS_FINALIZING,
    STATUS_PROPOSING,
    STATUS_READY_TO_FINALIZE,
    STATUS_WAITING_FOR_HUMAN,
    AdvisorStateError,
    accept_selection_report,
    begin_finalize,
    bind_advisor_session,
    bind_human_feedback,
    commit_problem_assignment,
    current_status,
    initialize_advisor_state,
    open_advisor_round,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
ROOT = "Prove that every object satisfying H has property P."


def memory_snapshot() -> AdvisorMemorySnapshot:
    return AdvisorMemorySnapshot(
        snapshot_id="MMS-0001",
        snapshot_digest=SHA_A,
        source_event_cursor=17,
        relative_path="input/main_memory_snapshot",
    )


def cycle_context(
    *,
    advisor_index: int = 1,
    previous_assignments: tuple[ProblemAssignment, ...] = (),
) -> AdvisorCycleContext:
    return AdvisorCycleContext(
        advisor_index=advisor_index,
        source_cycle=advisor_index,
        target_cycle=advisor_index + 1,
        original_problem=ROOT,
        memory_snapshot=memory_snapshot(),
        previous_assignments=previous_assignments,
    )


def obligation(
    rank: int,
    *,
    obligation_id: str | None = None,
    statement: str | None = None,
    previous_assignment_ids: tuple[str, ...] = (),
    repeat_justification: str | None = None,
    breakthrough_evidence_ids: tuple[str, ...] = (),
) -> RankedObligation:
    return RankedObligation(
        obligation_id=obligation_id or f"ADV-O-{rank}",
        rank=rank,
        title=f"Obligation {rank}",
        statement=statement or f"Prove the decisive reduction R_{rank}.",
        importance=f"R_{rank} controls a central obstruction.",
        landscape_change=f"Resolving R_{rank} eliminates a major branch of uncertainty.",
        relationship_to_root=f"R_{rank} supplies a missing implication toward ROOT.",
        novelty=f"R_{rank} is distinct from the previously tested mechanisms.",
        previous_assignment_ids=previous_assignment_ids,
        repeat_justification=repeat_justification,
        breakthrough_evidence_ids=breakthrough_evidence_ids,
    )


def selection_report(
    context: AdvisorCycleContext,
    *,
    obligations: tuple[RankedObligation, ...] | None = None,
) -> SelectionReport:
    return SelectionReport(
        selection_report_id=f"ASR-{context.advisor_index}",
        feedback_request_id=f"AFR-{context.advisor_index}",
        advisor_index=context.advisor_index,
        source_cycle=context.source_cycle,
        target_cycle=context.target_cycle,
        original_problem_digest=context.original_problem_digest,
        memory_snapshot_id=context.memory_snapshot.snapshot_id,
        memory_snapshot_digest=context.memory_snapshot.snapshot_digest,
        previous_assignments_digest=context.previous_assignments_digest,
        obligations=(
            obligations
            if obligations is not None
            else tuple(obligation(rank) for rank in range(1, 6))
        ),
        human_question="Which one or two obligations should guide the next cycle?",
    )


def human_feedback(
    report: SelectionReport,
    choices: tuple[FeedbackChoice, ...],
) -> HumanFeedback:
    return HumanFeedback(
        feedback_id=f"AHF-{report.advisor_index}",
        feedback_request_id=report.feedback_request_id,
        selection_report_id=report.selection_report_id,
        selection_report_digest=report.digest,
        choices=choices,
        instructions="Follow these choices exactly and preserve their order.",
    )


def prior_assignment(*, statement: str = "Prove the decisive reduction R_1.") -> ProblemAssignment:
    return ProblemAssignment(
        assignment_id="APA-1",
        advisor_index=1,
        source_cycle=1,
        target_cycle=2,
        original_problem=ROOT,
        selection_report_id="ASR-1",
        selection_report_digest=SHA_B,
        feedback_id="AHF-1",
        feedback_digest=SHA_C,
        subproblems=(
            SelectedSubproblem(
                source="listed",
                obligation_id="ADV-O-1",
                statement=statement,
                human_choice_index=1,
            ),
        ),
    )


class AdvisorContractTests(unittest.TestCase):
    def test_selection_report_requires_exactly_five_ranked_obligations(self) -> None:
        context = cycle_context()
        for count in (0, 1, 4, 6):
            candidates = tuple(
                obligation(rank) for rank in range(1, min(count, 5) + 1)
            )
            if count == 6:
                candidates += (
                    obligation(
                        5,
                        obligation_id="ADV-O-6",
                        statement="Prove the sixth independent reduction.",
                    ),
                )
            with self.subTest(count=count), self.assertRaises(AdvisorContractError) as raised:
                selection_report(
                    context,
                    obligations=candidates,
                )
            self.assertEqual(raised.exception.code, "invalid_obligation_count")

        wrong_order = tuple(
            obligation(rank, obligation_id=f"ADV-O-{index}")
            for index, rank in enumerate((1, 2, 4, 3, 5), start=1)
        )
        with self.assertRaises(AdvisorContractError) as raised:
            selection_report(context, obligations=wrong_order)
        self.assertEqual(raised.exception.code, "invalid_obligation_ranking")

    def test_selection_report_rejects_duplicate_ids_and_statements(self) -> None:
        context = cycle_context()
        duplicate_ids = tuple(
            obligation(rank, obligation_id="ADV-O-1" if rank == 2 else None)
            for rank in range(1, 6)
        )
        with self.assertRaises(AdvisorContractError) as raised:
            selection_report(context, obligations=duplicate_ids)
        self.assertEqual(raised.exception.code, "duplicate_obligation_id")

        duplicate_statements = tuple(
            obligation(rank, statement="Same mathematical obligation." if rank < 3 else None)
            for rank in range(1, 6)
        )
        with self.assertRaises(AdvisorContractError) as raised:
            selection_report(context, obligations=duplicate_statements)
        self.assertEqual(raised.exception.code, "duplicate_obligation_statement")

    def test_report_is_bound_to_the_exact_cycle_and_memory_snapshot(self) -> None:
        context = cycle_context()
        report = selection_report(context)
        report.validate_for_context(context)

        other = AdvisorCycleContext(
            advisor_index=1,
            source_cycle=1,
            target_cycle=2,
            original_problem=ROOT,
            memory_snapshot=AdvisorMemorySnapshot(
                snapshot_id="MMS-0002",
                snapshot_digest=SHA_B,
                source_event_cursor=18,
                relative_path="input/main_memory_snapshot",
            ),
        )
        with self.assertRaises(AdvisorContractError) as raised:
            report.validate_for_context(other)
        self.assertEqual(raised.exception.code, "selection_report_context_mismatch")

    def test_an_advisor_obligation_cannot_repeat_root_itself(self) -> None:
        context = cycle_context()
        obligations = (
            obligation(1, statement=ROOT),
            *(obligation(rank) for rank in range(2, 6)),
        )
        report = selection_report(context, obligations=obligations)
        with self.assertRaises(AdvisorContractError) as raised:
            report.validate_for_context(context)
        self.assertEqual(raised.exception.code, "obligation_duplicates_root")

    def test_repeated_obligation_requires_declared_history_and_breakthrough_metadata(self) -> None:
        previous = prior_assignment()
        context = cycle_context(advisor_index=2, previous_assignments=(previous,))
        repeated_statement = previous.subproblems[0].statement

        undeclared = tuple(
            obligation(rank, statement=repeated_statement if rank == 1 else None)
            for rank in range(1, 6)
        )
        report = selection_report(context, obligations=undeclared)
        with self.assertRaises(AdvisorContractError) as raised:
            report.validate_for_context(context)
        self.assertEqual(raised.exception.code, "undeclared_repeated_obligation")

        with self.assertRaises(AdvisorContractError):
            obligation(1, previous_assignment_ids=(previous.assignment_id,))
        with self.assertRaises(AdvisorContractError) as raised:
            obligation(
                1,
                previous_assignment_ids=(previous.assignment_id,),
                repeat_justification="A new lemma changes the tractability.",
            )
        self.assertEqual(raised.exception.code, "missing_breakthrough_evidence")

        declared = (
            obligation(
                1,
                statement=repeated_statement,
                previous_assignment_ids=(previous.assignment_id,),
                repeat_justification="A newly proved lemma removes the old obstruction.",
                breakthrough_evidence_ids=("F-NEW-LEMMA",),
            ),
            *(obligation(rank) for rank in range(2, 6)),
        )
        selection_report(context, obligations=declared).validate_for_context(context)

    def test_repeat_metadata_cannot_reference_an_unknown_assignment(self) -> None:
        previous = prior_assignment()
        context = cycle_context(advisor_index=2, previous_assignments=(previous,))
        obligations = (
            obligation(
                1,
                previous_assignment_ids=("APA-UNKNOWN",),
                repeat_justification="The landscape changed.",
                breakthrough_evidence_ids=("F-NEW",),
            ),
            *(obligation(rank) for rank in range(2, 6)),
        )
        with self.assertRaises(AdvisorContractError) as raised:
            selection_report(context, obligations=obligations).validate_for_context(context)
        self.assertEqual(raised.exception.code, "unknown_previous_assignment")

    def test_repeat_evidence_must_be_new_or_revision_advanced_since_assignment(
        self,
    ) -> None:
        previous = prior_assignment()
        context = cycle_context(advisor_index=2, previous_assignments=(previous,))
        obligations = (
            obligation(
                1,
                statement=previous.subproblems[0].statement,
                previous_assignment_ids=(previous.assignment_id,),
                repeat_justification=(
                    "A new revision removes the obstruction from the first cycle."
                ),
                breakthrough_evidence_ids=("F-BRIDGE",),
            ),
            *(obligation(rank) for rank in range(2, 6)),
        )
        report = selection_report(context, obligations=obligations)

        stale = build_breakthrough_evidence_freshness(
            context,
            current_revisions={"F-BRIDGE": 1},
            previous_revisions={previous.assignment_id: {"F-BRIDGE": 1}},
        )
        with self.assertRaises(AdvisorContractError) as raised:
            validate_breakthrough_evidence_freshness(
                report,
                context,
                current_revisions={"F-BRIDGE": 1},
                freshness=stale,
            )
        self.assertEqual(raised.exception.code, "stale_breakthrough_evidence")

        advanced = build_breakthrough_evidence_freshness(
            context,
            current_revisions={"F-BRIDGE": 2},
            previous_revisions={previous.assignment_id: {"F-BRIDGE": 1}},
        )
        validate_breakthrough_evidence_freshness(
            report,
            context,
            current_revisions={"F-BRIDGE": 2},
            freshness=advanced,
        )

        newly_created = build_breakthrough_evidence_freshness(
            context,
            current_revisions={"F-BRIDGE": 1},
            previous_revisions={previous.assignment_id: {}},
        )
        validate_breakthrough_evidence_freshness(
            report,
            context,
            current_revisions={"F-BRIDGE": 1},
            freshness=newly_created,
        )

    def test_evidence_freshness_threshold_handles_first_and_later_rounds(self) -> None:
        first_context = cycle_context()
        first_freshness = build_breakthrough_evidence_freshness(
            first_context,
            current_revisions={"F-FOUNDATION": 1},
            previous_revisions={},
        )
        self.assertEqual(first_freshness["records"], {})
        validate_breakthrough_evidence_freshness(
            selection_report(first_context),
            first_context,
            current_revisions={"F-FOUNDATION": 1},
            freshness=first_freshness,
        )

        first = prior_assignment()
        second = ProblemAssignment(
            assignment_id="APA-2",
            advisor_index=2,
            source_cycle=2,
            target_cycle=3,
            original_problem=ROOT,
            selection_report_id="ASR-2",
            selection_report_digest=SHA_B,
            feedback_id="AHF-2",
            feedback_digest=SHA_C,
            subproblems=(
                SelectedSubproblem(
                    source="listed",
                    obligation_id="ADV-O-2",
                    statement="Prove an independent second-cycle reduction.",
                    human_choice_index=1,
                ),
            ),
        )
        context = cycle_context(
            advisor_index=3, previous_assignments=(first, second)
        )
        freshness = build_breakthrough_evidence_freshness(
            context,
            current_revisions={"F-BRIDGE": 2},
            previous_revisions={
                first.assignment_id: {"F-BRIDGE": 1},
                second.assignment_id: {"F-BRIDGE": 2},
            },
        )
        self.assertEqual(
            freshness["records"]["F-BRIDGE"]["newer_than_advisor_index"],
            1,
        )

        repeats_first = (
            obligation(
                1,
                statement=first.subproblems[0].statement,
                previous_assignment_ids=(first.assignment_id,),
                repeat_justification="Revision two removes the original obstruction.",
                breakthrough_evidence_ids=("F-BRIDGE",),
            ),
            *(obligation(rank) for rank in range(2, 6)),
        )
        validate_breakthrough_evidence_freshness(
            selection_report(context, obligations=repeats_first),
            context,
            current_revisions={"F-BRIDGE": 2},
            freshness=freshness,
        )

        also_claims_second = (
            obligation(
                1,
                statement=first.subproblems[0].statement,
                previous_assignment_ids=(first.assignment_id, second.assignment_id),
                repeat_justification="The same revision is claimed for both rounds.",
                breakthrough_evidence_ids=("F-BRIDGE",),
            ),
            *(obligation(rank) for rank in range(2, 6)),
        )
        with self.assertRaises(AdvisorContractError) as raised:
            validate_breakthrough_evidence_freshness(
                selection_report(context, obligations=also_claims_second),
                context,
                current_revisions={"F-BRIDGE": 2},
                freshness=freshness,
            )
        self.assertEqual(raised.exception.code, "stale_breakthrough_evidence")

    def test_feedback_accepts_one_or_two_listed_or_custom_choices(self) -> None:
        report = selection_report(cycle_context())
        cases = (
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-3"),),
            (
                FeedbackChoice(kind="listed", obligation_id="ADV-O-2"),
                FeedbackChoice(kind="custom", statement="Prove the human-specified bridge lemma."),
            ),
        )
        for choices in cases:
            with self.subTest(choices=choices):
                human_feedback(report, choices).validate_for_report(report)

        for choices in ((), tuple(FeedbackChoice(kind="listed", obligation_id=f"ADV-O-{i}") for i in range(1, 4))):
            with self.subTest(invalid_count=len(choices)), self.assertRaises(
                AdvisorContractError
            ) as raised:
                human_feedback(report, choices)
            self.assertEqual(raised.exception.code, "invalid_feedback_choice_count")

    def test_feedback_rejects_an_unknown_listed_obligation(self) -> None:
        report = selection_report(cycle_context())
        feedback = human_feedback(
            report,
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-UNKNOWN"),),
        )
        with self.assertRaises(AdvisorContractError) as raised:
            feedback.validate_for_report(report)
        self.assertEqual(raised.exception.code, "unknown_feedback_obligation")

    def test_feedback_rejects_unknown_fields_instead_of_ignoring_human_input(self) -> None:
        with self.assertRaises(AdvisorContractError) as raised:
            FeedbackChoice.from_dict(
                {
                    "kind": "listed",
                    "obligation_id": "ADV-O-1",
                    "statement": None,
                    "obligation": "misspelled field",
                }
            )
        self.assertEqual(raised.exception.code, "unknown_feedback_choice_field")

        report = selection_report(cycle_context())
        payload = human_feedback(
            report,
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-1"),),
        ).to_dict()
        payload["extra_instruction"] = "Do not silently discard this field."
        with self.assertRaises(AdvisorContractError) as raised:
            HumanFeedback.from_dict(payload)
        self.assertEqual(raised.exception.code, "invalid_human_feedback_fields")

    def test_finalization_must_follow_listed_and_custom_feedback_exactly(self) -> None:
        context = cycle_context()
        report = selection_report(context)
        feedback = human_feedback(
            report,
            (
                FeedbackChoice(kind="listed", obligation_id="ADV-O-2"),
                FeedbackChoice(kind="custom", statement="Prove the custom obstruction theorem."),
            ),
        )
        correct = AdvisorFinalization(
            selection_report_id=report.selection_report_id,
            selection_report_digest=report.digest,
            feedback_id=feedback.feedback_id,
            feedback_digest=feedback.digest,
            selected_subproblems=(
                SelectedSubproblem(
                    source="listed",
                    obligation_id="ADV-O-2",
                    statement=report.obligations[1].statement,
                    human_choice_index=1,
                ),
                SelectedSubproblem(
                    source="human_override",
                    statement="Prove the custom obstruction theorem.",
                    human_choice_index=2,
                ),
            ),
        )
        correct.validate_bindings(report, feedback)

        changed = AdvisorFinalization(
            selection_report_id=report.selection_report_id,
            selection_report_digest=report.digest,
            feedback_id=feedback.feedback_id,
            feedback_digest=feedback.digest,
            selected_subproblems=(
                SelectedSubproblem(
                    source="listed",
                    obligation_id="ADV-O-2",
                    statement="A model-rewritten version of the listed obligation.",
                    human_choice_index=1,
                ),
                correct.selected_subproblems[1],
            ),
        )
        with self.assertRaises(AdvisorContractError) as raised:
            changed.validate_bindings(report, feedback)
        self.assertEqual(raised.exception.code, "listed_feedback_not_followed")

    def test_problem_text_is_deterministic_and_omits_an_empty_second_section(self) -> None:
        one = SelectedSubproblem(
            source="listed",
            obligation_id="ADV-O-1",
            statement="Prove the first reduction.",
            human_choice_index=1,
        )
        expected_one = (
            "Solve the Original Problem. For this research cycle, you *should* attack "
            "the Original Problem by first solving the Subproblem(s), and treat the "
            "Subproblem(s) also as the *primary objective*. You may switch to another "
            "route *only after* recording concrete evidence that the assigned "
            "Subproblem(s) is materially less promising, and that a named alternative "
            "is substantially more likely to produce decisive progress toward the "
            "Original Problem.\n\n"
            f"Original Problem:\n{ROOT}\n\n"
            "Subproblem 1:\nProve the first reduction.\n"
        )
        self.assertEqual(render_problem_assignment(ROOT, (one,)), expected_one)
        self.assertNotIn("Subproblem 2:", expected_one)

        two = SelectedSubproblem(
            source="human_override",
            statement="Construct the requested counterexample.",
            human_choice_index=2,
        )
        rendered = render_problem_assignment(ROOT, (one, two))
        self.assertEqual(rendered.count("Original Problem:"), 1)
        self.assertTrue(rendered.endswith("Subproblem 2:\nConstruct the requested counterexample.\n"))

    def test_legacy_problem_assignment_text_remains_loadable(self) -> None:
        one = SelectedSubproblem(
            source="listed",
            obligation_id="ADV-O-1",
            statement="Prove the first reduction.",
            human_choice_index=1,
        )
        assignment = ProblemAssignment(
            assignment_id="ADVISOR-ASSIGNMENT-00000002",
            advisor_index=1,
            source_cycle=1,
            target_cycle=2,
            original_problem=ROOT,
            selection_report_id="SR-0001",
            selection_report_digest=SHA_A,
            feedback_id="FR-0001",
            feedback_digest=SHA_B,
            subproblems=(one,),
        )
        payload = assignment.to_dict()
        payload["problem_text"] = (
            "The task is to solve the original problem, and you are encouraged to "
            "attack it by solving the subproblems.\n\n"
            f"Original Problem:\n{ROOT}\n\n"
            "Subproblem 1:\nProve the first reduction.\n"
        )
        restored = ProblemAssignment.from_dict(payload)
        self.assertEqual(restored.problem_text, payload["problem_text"])

    def test_build_problem_assignment_is_bound_and_round_trip_idempotent(self) -> None:
        context = cycle_context()
        report = selection_report(context)
        feedback = human_feedback(
            report,
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-4"),),
        )
        finalization = AdvisorFinalization(
            selection_report_id=report.selection_report_id,
            selection_report_digest=report.digest,
            feedback_id=feedback.feedback_id,
            feedback_digest=feedback.digest,
            selected_subproblems=(
                SelectedSubproblem(
                    source="listed",
                    obligation_id="ADV-O-4",
                    statement=report.obligations[3].statement,
                    human_choice_index=1,
                ),
            ),
        )
        first = build_problem_assignment(
            assignment_id="APA-1",
            context=context,
            report=report,
            feedback=feedback,
            finalization=finalization,
        )
        second = build_problem_assignment(
            assignment_id="APA-1",
            context=context,
            report=report,
            feedback=feedback,
            finalization=finalization,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(ProblemAssignment.from_dict(copy.deepcopy(first.to_dict())), first)
        self.assertEqual(first.target_cycle, 2)

        bad_payload = first.to_dict()
        bad_payload["problem_text"] = "Agent-authored drift.\n"
        with self.assertRaises(AdvisorContractError) as raised:
            ProblemAssignment.from_dict(bad_payload)
        self.assertEqual(raised.exception.code, "problem_text_mismatch")


class AdvisorStateTests(unittest.TestCase):
    def _complete_first_round(self):  # type: ignore[no-untyped-def]
        context = cycle_context()
        report = selection_report(context)
        feedback = human_feedback(
            report,
            (
                FeedbackChoice(kind="listed", obligation_id="ADV-O-1"),
                FeedbackChoice(
                    kind="custom",
                    statement="Establish the operator-selected comparison theorem.",
                ),
            ),
        )
        finalization = AdvisorFinalization(
            selection_report_id=report.selection_report_id,
            selection_report_digest=report.digest,
            feedback_id=feedback.feedback_id,
            feedback_digest=feedback.digest,
            selected_subproblems=(
                SelectedSubproblem(
                    source="listed",
                    obligation_id="ADV-O-1",
                    statement=report.obligations[0].statement,
                    human_choice_index=1,
                ),
                SelectedSubproblem(
                    source="human_override",
                    statement="Establish the operator-selected comparison theorem.",
                    human_choice_index=2,
                ),
            ),
        )

        initial = initialize_advisor_state()
        bound = bind_advisor_session(initial, session_id="thread-advisor-project").state
        opened = open_advisor_round(
            bound, context, proposal_call_id="CALL-ADVISOR-PROPOSE-1"
        ).state
        waiting = accept_selection_report(
            opened,
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
            report=report,
        ).state
        ready = bind_human_feedback(waiting, feedback).state
        finalizing = begin_finalize(
            ready, finalize_call_id="CALL-ADVISOR-FINALIZE-1"
        ).state
        committed = commit_problem_assignment(
            finalizing,
            finalize_call_id="CALL-ADVISOR-FINALIZE-1",
            assignment_id="APA-1",
            finalization=finalization,
        )
        return {
            "context": context,
            "report": report,
            "feedback": feedback,
            "finalization": finalization,
            "initial": initial,
            "bound": bound,
            "opened": opened,
            "waiting": waiting,
            "ready": ready,
            "finalizing": finalizing,
            "committed": committed,
        }

    def test_lifecycle_is_pure_and_waits_for_feedback_before_finalization(self) -> None:
        context = cycle_context()
        report = selection_report(context)
        initial = initialize_advisor_state()
        original = copy.deepcopy(initial)

        opened_transition = open_advisor_round(
            initial, context, proposal_call_id="CALL-ADVISOR-PROPOSE-1"
        )
        self.assertEqual(initial, original)
        self.assertTrue(opened_transition.changed)
        self.assertEqual(current_status(opened_transition.state), STATUS_PROPOSING)

        waiting_transition = accept_selection_report(
            opened_transition.state,
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
            report=report,
        )
        self.assertEqual(current_status(opened_transition.state), STATUS_PROPOSING)
        self.assertEqual(current_status(waiting_transition.state), STATUS_WAITING_FOR_HUMAN)
        self.assertEqual(
            [event["event"] for event in waiting_transition.events],
            ["advisor_selection_report_accepted", "advisor_waiting_for_human"],
        )

        with self.assertRaises(AdvisorStateError) as raised:
            begin_finalize(
                waiting_transition.state,
                finalize_call_id="CALL-ADVISOR-FINALIZE-1",
            )
        self.assertEqual(raised.exception.code, "finalize_not_ready")

        feedback = human_feedback(
            report,
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-5"),),
        )
        ready_transition = bind_human_feedback(waiting_transition.state, feedback)
        self.assertEqual(current_status(ready_transition.state), STATUS_READY_TO_FINALIZE)
        with self.assertRaises(AdvisorStateError) as raised:
            begin_finalize(
                ready_transition.state,
                finalize_call_id="CALL-ADVISOR-FINALIZE-1",
            )
        self.assertEqual(raised.exception.code, "advisor_session_unbound")

    def test_session_binding_is_project_wide_immutable_and_idempotent(self) -> None:
        state = initialize_advisor_state(session_key="advisor:project")
        first = bind_advisor_session(state, session_id="thread-advisor-project")
        self.assertTrue(first.changed)
        self.assertEqual(state["session_id"], None)
        self.assertEqual(first.state["session_id"], "thread-advisor-project")

        replay = bind_advisor_session(
            first.state, session_id="thread-advisor-project"
        )
        self.assertFalse(replay.changed)
        self.assertEqual(replay.state, first.state)
        with self.assertRaises(AdvisorStateError) as raised:
            bind_advisor_session(first.state, session_id="thread-replacement")
        self.assertEqual(
            raised.exception.code, "advisor_session_replacement_forbidden"
        )

    def test_every_transition_is_exactly_replay_idempotent(self) -> None:
        completed = self._complete_first_round()

        reopened = open_advisor_round(
            completed["bound"],
            completed["context"],
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
        )
        self.assertTrue(reopened.changed)
        open_replay = open_advisor_round(
            reopened.state,
            completed["context"],
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
        )
        self.assertFalse(open_replay.changed)
        self.assertEqual(open_replay.state, reopened.state)

        report_replay = accept_selection_report(
            completed["waiting"],
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
            report=completed["report"],
        )
        self.assertFalse(report_replay.changed)
        self.assertEqual(report_replay.state, completed["waiting"])

        feedback_replay = bind_human_feedback(
            completed["ready"], completed["feedback"]
        )
        self.assertFalse(feedback_replay.changed)
        self.assertEqual(feedback_replay.state, completed["ready"])

        finalize_replay = begin_finalize(
            completed["finalizing"],
            finalize_call_id="CALL-ADVISOR-FINALIZE-1",
        )
        self.assertFalse(finalize_replay.changed)
        self.assertEqual(finalize_replay.state, completed["finalizing"])

        commit_replay = commit_problem_assignment(
            completed["committed"].state,
            finalize_call_id="CALL-ADVISOR-FINALIZE-1",
            assignment_id="APA-1",
            finalization=completed["finalization"],
        )
        self.assertFalse(commit_replay.changed)
        self.assertEqual(commit_replay.state, completed["committed"].state)
        self.assertEqual(commit_replay.value, completed["committed"].value)

    def test_selection_report_and_feedback_cannot_be_replaced(self) -> None:
        context = cycle_context()
        report = selection_report(context)
        state = open_advisor_round(
            initialize_advisor_state(),
            context,
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
        ).state
        waiting = accept_selection_report(
            state,
            proposal_call_id="CALL-ADVISOR-PROPOSE-1",
            report=report,
        ).state
        replacement_report = SelectionReport(
            **{
                **report.to_dict(),
                "selection_report_id": "ASR-REPLACEMENT",
                "obligations": report.obligations,
            }
        )
        with self.assertRaises(AdvisorStateError) as raised:
            accept_selection_report(
                waiting,
                proposal_call_id="CALL-ADVISOR-PROPOSE-1",
                report=replacement_report,
            )
        self.assertEqual(
            raised.exception.code, "selection_report_replacement_forbidden"
        )

        feedback = human_feedback(
            report,
            (FeedbackChoice(kind="listed", obligation_id="ADV-O-1"),),
        )
        ready = bind_human_feedback(waiting, feedback).state
        replacement_feedback = HumanFeedback(
            feedback_id="AHF-REPLACEMENT",
            feedback_request_id=report.feedback_request_id,
            selection_report_id=report.selection_report_id,
            selection_report_digest=report.digest,
            choices=(FeedbackChoice(kind="listed", obligation_id="ADV-O-2"),),
            instructions="Use the replacement choice.",
        )
        with self.assertRaises(AdvisorStateError) as raised:
            bind_human_feedback(ready, replacement_feedback)
        self.assertEqual(raised.exception.code, "feedback_not_expected")

    def test_completed_round_opens_the_next_only_with_exact_assignment_history(self) -> None:
        completed = self._complete_first_round()
        state = completed["committed"].state
        assignment = completed["committed"].value
        self.assertEqual(current_status(state), "idle")
        self.assertEqual(len(state["history"]), 1)

        next_context = cycle_context(
            advisor_index=2, previous_assignments=(assignment,)
        )
        opened = open_advisor_round(
            state, next_context, proposal_call_id="CALL-ADVISOR-PROPOSE-2"
        )
        self.assertTrue(opened.changed)
        self.assertEqual(opened.state["active"]["advisor_index"], 2)
        self.assertEqual(opened.state["session_id"], "thread-advisor-project")

        wrong_context = cycle_context(advisor_index=2, previous_assignments=())
        with self.assertRaises(AdvisorStateError) as raised:
            open_advisor_round(
                state,
                wrong_context,
                proposal_call_id="CALL-ADVISOR-PROPOSE-WRONG-HISTORY",
            )
        self.assertEqual(
            raised.exception.code, "previous_assignment_history_mismatch"
        )

        out_of_order = AdvisorCycleContext(
            advisor_index=3,
            source_cycle=3,
            target_cycle=4,
            original_problem=ROOT,
            memory_snapshot=memory_snapshot(),
            previous_assignments=(assignment,),
        )
        with self.assertRaises(AdvisorStateError) as raised:
            open_advisor_round(
                state,
                out_of_order,
                proposal_call_id="CALL-ADVISOR-PROPOSE-3",
            )
        self.assertEqual(raised.exception.code, "advisor_index_out_of_order")

    def test_report_and_feedback_request_ids_cannot_be_reused_across_rounds(self) -> None:
        completed = self._complete_first_round()
        assignment = completed["committed"].value
        next_context = cycle_context(
            advisor_index=2, previous_assignments=(assignment,)
        )
        next_report = selection_report(
            next_context,
            obligations=tuple(
                obligation(
                    rank,
                    statement=f"Prove the cycle-two reduction S_{rank}.",
                )
                for rank in range(1, 6)
            ),
        )

        cases = (
            (
                replace(
                    next_report,
                    selection_report_id=completed["report"].selection_report_id,
                ),
                "duplicate_selection_report_id",
            ),
            (
                replace(
                    next_report,
                    feedback_request_id=completed["report"].feedback_request_id,
                ),
                "duplicate_feedback_request_id",
            ),
        )
        for report, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                opened = open_advisor_round(
                    completed["committed"].state,
                    next_context,
                    proposal_call_id=f"CALL-{expected_code}",
                ).state
                with self.assertRaises(AdvisorStateError) as raised:
                    accept_selection_report(
                        opened,
                        proposal_call_id=f"CALL-{expected_code}",
                        report=report,
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_durable_state_round_trips_as_json_between_transitions(self) -> None:
        completed = self._complete_first_round()
        durable = json.loads(json.dumps(completed["committed"].state))
        self.assertEqual(current_status(durable), "idle")
        assignment = ProblemAssignment.from_dict(
            durable["history"][0]["problem_assignment"]
        )
        next_context = cycle_context(
            advisor_index=2, previous_assignments=(assignment,)
        )
        opened = open_advisor_round(
            durable,
            next_context,
            proposal_call_id="CALL-ADVISOR-PROPOSE-2",
        )
        self.assertEqual(current_status(opened.state), STATUS_PROPOSING)
        self.assertEqual(durable["active"], None)

    def test_committed_finalize_call_rejects_assignment_id_replacement(self) -> None:
        completed = self._complete_first_round()
        with self.assertRaises(AdvisorStateError) as raised:
            commit_problem_assignment(
                completed["committed"].state,
                finalize_call_id="CALL-ADVISOR-FINALIZE-1",
                assignment_id="APA-REPLACEMENT",
                finalization=completed["finalization"],
            )
        self.assertEqual(
            raised.exception.code, "assignment_replacement_forbidden"
        )

    def test_committed_finalize_call_rejects_finalization_drift(self) -> None:
        completed = self._complete_first_round()
        original = completed["finalization"]
        drifted = AdvisorFinalization(
            selection_report_id=original.selection_report_id,
            selection_report_digest=original.selection_report_digest,
            feedback_id=original.feedback_id,
            feedback_digest=original.feedback_digest,
            selected_subproblems=(original.selected_subproblems[0],),
        )
        with self.assertRaises(AdvisorStateError) as raised:
            commit_problem_assignment(
                completed["committed"].state,
                finalize_call_id="CALL-ADVISOR-FINALIZE-1",
                assignment_id="APA-1",
                finalization=drifted,
            )
        self.assertEqual(
            raised.exception.code, "assignment_replacement_forbidden"
        )


if __name__ == "__main__":
    unittest.main()
