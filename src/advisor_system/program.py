"""Transport-neutral two-call Advisor orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import AdvisorCycleContext, ProblemAssignment, SelectionReport
from .interfaces import AdvisorHost
from .prompts import build_launch_spec
from .settings import AdvisorSettings
from .state import (
    STATUS_FINALIZING,
    STATUS_PROPOSING,
    STATUS_READY_TO_FINALIZE,
    STATUS_WAITING_FOR_HUMAN,
    AdvisorStateError,
    current_status,
)


@dataclass(frozen=True)
class AdvisorAdvance:
    """Observable outcome of advancing until the next external boundary."""

    handled: bool
    progressed: bool
    status: str
    waiting_for_human: bool = False
    selection_report: SelectionReport | None = None
    problem_assignment: ProblemAssignment | None = None


class AdvisorProgram:
    """Drive proposal, hard human pause, resumed finalization, and commit."""

    def __init__(self, host: AdvisorHost, *, settings: AdvisorSettings | None = None) -> None:
        self._host = host
        self._settings = settings or AdvisorSettings()
        self._settings.validate()

    def advance(self, context: AdvisorCycleContext | None = None) -> AdvisorAdvance:
        """Advance one round to its mandatory pause or final assignment.

        A proposal is always returned to the scheduler immediately after the
        selection report is accepted.  The same invocation never consumes
        feedback, which makes the human boundary explicit even for a host that
        already has queued input.  A later invocation may bind feedback and run
        the resumed finalization call in one pass.
        """

        state = self._host.advisor_state_snapshot()
        status = current_status(state)
        progressed = False

        if status == "idle":
            if context is None:
                return AdvisorAdvance(False, False, "idle")
            self._host.open_advisor_round(context)
            progressed = True
            state = self._host.advisor_state_snapshot()
            status = current_status(state)
            if status != STATUS_PROPOSING:
                raise AdvisorStateError(
                    "host did not persist the opened proposal call",
                    code="host_transition_missing",
                )
        elif context is not None:
            active = state.get("active")
            if not isinstance(active, dict) or active.get("context_digest") != context.digest:
                raise AdvisorStateError(
                    "supplied context differs from the active Advisor round",
                    code="active_context_mismatch",
                )

        active = state.get("active")
        if not isinstance(active, dict):
            raise AdvisorStateError("host state has no active round", code="invalid_state")
        advisor_index = active.get("advisor_index")

        if status == STATUS_PROPOSING:
            call_id = active.get("proposal_call_id")
            if not isinstance(call_id, str):
                raise AdvisorStateError("proposal call ID is missing", code="invalid_state")
            response = self._host.execute_advisor_call(
                call_id,
                build_launch_spec(
                    stage="proposal",
                    advisor_index=advisor_index,
                    settings=self._settings,
                ),
            )
            report = self._host.accept_proposal_response(call_id, response)
            return AdvisorAdvance(
                True,
                True,
                STATUS_WAITING_FOR_HUMAN,
                waiting_for_human=True,
                selection_report=report,
            )

        if status == STATUS_WAITING_FOR_HUMAN:
            report = SelectionReport.from_dict(active["selection_report"])
            feedback = self._host.human_feedback_for(report.feedback_request_id)
            if feedback is None:
                return AdvisorAdvance(
                    True,
                    progressed,
                    STATUS_WAITING_FOR_HUMAN,
                    waiting_for_human=True,
                    selection_report=report,
                )
            self._host.bind_human_feedback(feedback)
            progressed = True
            state = self._host.advisor_state_snapshot()
            status = current_status(state)
            active = state.get("active")
            if status != STATUS_READY_TO_FINALIZE or not isinstance(active, dict):
                raise AdvisorStateError(
                    "host did not bind the human feedback",
                    code="host_transition_missing",
                )

        if status == STATUS_READY_TO_FINALIZE:
            self._host.begin_finalize_call()
            progressed = True
            state = self._host.advisor_state_snapshot()
            status = current_status(state)
            active = state.get("active")
            if status != STATUS_FINALIZING or not isinstance(active, dict):
                raise AdvisorStateError(
                    "host did not persist the finalize call",
                    code="host_transition_missing",
                )

        if status == STATUS_FINALIZING:
            call_id = active.get("finalize_call_id")
            advisor_index = active.get("advisor_index")
            if not isinstance(call_id, str):
                raise AdvisorStateError("finalize call ID is missing", code="invalid_state")
            response = self._host.execute_advisor_call(
                call_id,
                build_launch_spec(
                    stage="finalize",
                    advisor_index=advisor_index,
                    settings=self._settings,
                ),
            )
            assignment = self._host.accept_finalize_response(call_id, response)
            return AdvisorAdvance(
                True,
                True,
                "completed",
                problem_assignment=assignment,
            )

        raise AdvisorStateError(
            f"unsupported active Advisor status: {status!r}", code="invalid_state"
        )


__all__ = ["AdvisorAdvance", "AdvisorProgram"]
