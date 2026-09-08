"""Fixed, host-independent Explorer worker program."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import time
from typing import Any, Callable, Mapping

from . import control
from .contracts import ExplorerHandoff
from .interfaces import ExplorerCollaborator, ExplorerHandoffFactory, ExplorerHost


class ExplorerProgramError(RuntimeError):
    """The host violated the Explorer program interface."""


@dataclass(frozen=True)
class ExplorerTurnAdvance:
    """Result of one complete portable turn-controller advancement."""

    handled: bool
    progressed: bool
    handoff: ExplorerHandoff | None = None
    collaborator_receipt: Any = None


class ExplorerProgram:
    """Run concurrent free-thinking attempts through a narrow host port.

    Explorer owns admission planning, worker-wave execution, attempt isolation,
    deadline polling, proof-candidate fan-out cancellation, and failure
    containment.  The host port supplies durable primitives and agent launch.
    """

    def __init__(
        self,
        host: ExplorerHost,
        *,
        service: ExplorerHandoffFactory | None = None,
        collaborator: ExplorerCollaborator | None = None,
        poll_seconds: float = 0.2,
        continuous_refill: bool = False,
        executor_factory: Callable[..., ThreadPoolExecutor] = ThreadPoolExecutor,
    ) -> None:
        if not isinstance(poll_seconds, (int, float)) or poll_seconds < 0:
            raise ValueError("poll_seconds must be nonnegative")
        if not isinstance(continuous_refill, bool):
            raise ValueError("continuous_refill must be a boolean")
        self.host = host
        self.service = service
        self.collaborator = collaborator
        self.poll_seconds = float(poll_seconds)
        self.continuous_refill = continuous_refill
        self.executor_factory = executor_factory

    def _expire(self) -> tuple[str, ...]:
        expired = tuple(str(item) for item in self.host.expire_attempts())
        for call_id in expired:
            self.host.cancel_call(
                call_id,
                reason="Explorer planned-attempt deadline",
            )
        return expired

    def _plan_calls(
        self,
        *,
        max_calls: int,
        blocked_lineage_ids: frozenset[str] = frozenset(),
    ) -> tuple[tuple[str, str], ...]:
        """Plan launchable calls without overlapping blocked physical workers."""

        if not isinstance(max_calls, int) or isinstance(max_calls, bool):
            raise ValueError("max_calls must be an integer")
        if max_calls <= 0:
            return ()

        self.host.tick()
        planned: list[tuple[str, str]] = []
        planned_lineages: set[str] = set()
        while len(planned) < max_calls:
            snapshot = self.host.controller_snapshot()
            lineages = snapshot.get("lineages", {})
            if not isinstance(lineages, Mapping):
                raise ExplorerProgramError("Explorer controller lacks lineages")

            refresh = False
            for raw_lineage_id in snapshot.get("admission_order", []):
                if len(planned) >= max_calls:
                    break
                lineage_id = str(raw_lineage_id)
                if (
                    lineage_id in blocked_lineage_ids
                    or lineage_id in planned_lineages
                ):
                    continue
                lineage = lineages.get(lineage_id, {})
                if not isinstance(lineage, Mapping):
                    raise ExplorerProgramError("Explorer lineage is malformed")
                current_call_id = lineage.get("current_call_id")
                if current_call_id:
                    call_id = str(current_call_id)
                    call = self.host.call_snapshot(call_id)
                    if call.get("status") == "needs_attention":
                        failures = call.get("failures") or []
                        reason = "Explorer call exhausted its retry budget"
                        if failures and isinstance(failures[-1], Mapping):
                            reason = str(failures[-1].get("reason") or reason)
                        self.host.fail_attempt(call_id, reason=reason)
                        refresh = True
                        break
                    if call.get("status") in {
                        "prepared",
                        "retry_pending",
                        "completed",
                    }:
                        planned.append((lineage_id, call_id))
                        planned_lineages.add(lineage_id)
                    continue
                if lineage.get("status") not in {"ready", "continuation_pending"}:
                    continue
                call_id = self.host.start_attempt(
                    lineage_id,
                    source_high_water_seq=self.host.visible_high_water(),
                )
                planned.append((lineage_id, call_id))
                planned_lineages.add(lineage_id)

            if refresh:
                continue
            if len(planned) >= max_calls:
                break

            snapshot = self.host.controller_snapshot()
            if (
                self.host.phase == "explorer_admission"
                and control.available_slots(
                    snapshot, max_slots=self.host.max_workers
                )
                > 0
                and self.host.admit_lineage()
            ):
                continue
            break
        return tuple(planned)

    def _plan_wave(self) -> tuple[str, ...]:
        """Own lineage admission and one-attempt-per-ready-lineage planning."""

        return tuple(
            call_id
            for _lineage_id, call_id in self._plan_calls(
                max_calls=self.host.max_workers
            )
        )

    @staticmethod
    def _candidate(
        snapshot: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, str] | None:
        scratch_id = result.get("root_candidate_scratch_id")
        if scratch_id is None:
            return None
        payload = snapshot.get("input")
        if not isinstance(payload, Mapping):
            raise ExplorerProgramError("Explorer call snapshot lacks input")
        turn_id = str(payload.get("explorer_turn_id") or "")
        outcome = str(result.get("root_candidate_outcome") or "")
        if not turn_id or not outcome:
            raise ExplorerProgramError("Explorer root candidate is incomplete")
        candidate_id = "XCAND-" + hashlib.sha256(
            f"{turn_id}:{scratch_id}".encode("utf-8")
        ).hexdigest()[:24]
        return {
            "candidate_id": candidate_id,
            "scratch_id": str(scratch_id),
            "candidate_outcome": outcome,
        }

    def run_wave(self) -> bool:
        """Run one dynamically filled Explorer wave.

        The method is restart-safe because every durable decision is delegated
        to idempotent host-port methods.  Individual worker failures consume
        only their own planned attempt and never abort peer workers.
        """

        if self.continuous_refill:
            return self._run_continuously_refilled_wave()

        expired_before_launch = self._expire()
        call_ids = self._plan_wave()
        newly_expired = self._expire()
        call_ids = tuple(
            call_id for call_id in call_ids if self.host.call_is_launchable(call_id)
        )
        if not call_ids:
            return bool(expired_before_launch or newly_expired)

        launches = {
            call_id: self.host.prepare_launch(call_id) for call_id in call_ids
        }
        futures: dict[str, Future[Mapping[str, Any]]] = {}
        with self.executor_factory(max_workers=len(launches)) as pool:
            for call_id, launch in launches.items():
                futures[call_id] = pool.submit(
                    self.host.run_launch, call_id, launch
                )
            unfinished = set(futures)
            while unfinished:
                self.host.tick()
                self._expire()
                for call_id in list(unfinished):
                    future = futures[call_id]
                    if not future.done():
                        continue
                    unfinished.remove(call_id)
                    snapshot = self.host.call_snapshot(call_id)
                    if snapshot.get("status") == "cancelled":
                        try:
                            future.result()
                        except Exception:
                            pass
                        continue
                    try:
                        result = future.result()
                        self.host.validate_result(call_id, result)
                    except Exception as exc:
                        self.host.fail_attempt(
                            call_id,
                            reason=(
                                "explorer_attempt_error: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                        continue
                    cancellations = self.host.commit_attempt(
                        call_id,
                        outcome=(
                            "timed_out"
                            if result.get("stop_reason") == "time_limit"
                            else "progress"
                        ),
                        root_candidate=self._candidate(snapshot, result),
                    )
                    for other_id in cancellations:
                        self.host.cancel_call(
                            str(other_id),
                            reason="Explorer root candidate was recorded",
                        )
                if unfinished and self.poll_seconds:
                    time.sleep(self.poll_seconds)
        self.host.tick()
        return True

    def _run_continuously_refilled_wave(self) -> bool:
        """Keep physical slots full while preserving per-lineage serialization."""

        progressed = bool(self._expire())
        in_flight: dict[str, tuple[str, Future[Mapping[str, Any]]]] = {}
        with self.executor_factory(max_workers=self.host.max_workers) as pool:
            while True:
                self.host.tick()
                expired = self._expire()
                progressed = bool(expired) or progressed

                for call_id in tuple(in_flight):
                    _lineage_id, future = in_flight[call_id]
                    if not future.done():
                        continue
                    del in_flight[call_id]
                    progressed = True
                    snapshot = self.host.call_snapshot(call_id)
                    if snapshot.get("status") == "cancelled":
                        try:
                            future.result()
                        except Exception:
                            pass
                        continue
                    try:
                        result = future.result()
                        self.host.validate_result(call_id, result)
                    except Exception as exc:
                        self.host.fail_attempt(
                            call_id,
                            reason=(
                                "explorer_attempt_error: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                        continue
                    cancellations = self.host.commit_attempt(
                        call_id,
                        outcome=(
                            "timed_out"
                            if result.get("stop_reason") == "time_limit"
                            else "progress"
                        ),
                        root_candidate=self._candidate(snapshot, result),
                    )
                    for other_id in cancellations:
                        self.host.cancel_call(
                            str(other_id),
                            reason="Explorer root candidate was recorded",
                        )

                launch_capacity = self.host.max_workers - len(in_flight)
                planned: tuple[tuple[str, str], ...] = ()
                newly_expired: tuple[str, ...] = ()
                if launch_capacity > 0:
                    planned = self._plan_calls(
                        max_calls=launch_capacity,
                        blocked_lineage_ids=frozenset(
                            lineage_id for lineage_id, _future in in_flight.values()
                        ),
                    )
                    newly_expired = self._expire()
                    progressed = bool(newly_expired) or progressed
                    for lineage_id, call_id in planned:
                        if len(in_flight) >= self.host.max_workers:
                            break
                        if call_id in in_flight or not self.host.call_is_launchable(
                            call_id
                        ):
                            continue
                        launch = self.host.prepare_launch(call_id)
                        in_flight[call_id] = (
                            lineage_id,
                            pool.submit(self.host.run_launch, call_id, launch),
                        )
                        progressed = True

                if not in_flight:
                    if expired or newly_expired:
                        continue
                    self.host.tick()
                    return progressed
                if self.poll_seconds:
                    time.sleep(self.poll_seconds)

    def advance_turn(self) -> ExplorerTurnAdvance:
        """Advance the entire Explorer side of one alternating turn.

        This is the only entry point an outer collaborator loop needs. It owns
        admission/deadline ticking, worker-wave execution, graceful drain, the
        immutable repository freeze, handoff construction, and idempotent
        collaborator acceptance. A false ``handled`` result means the current
        phase belongs to the collaborator.
        """

        if self.service is None or self.collaborator is None:
            raise ExplorerProgramError(
                "complete turn advancement requires an Explorer service and collaborator"
            )
        if self.host.phase not in {"explorer_admission", "explorer_drain"}:
            return ExplorerTurnAdvance(handled=False, progressed=False)

        self.host.tick()
        progressed = self.run_wave()
        self.host.tick()
        if self.host.phase != "explorer_drain" or not self.host.explorer_is_drained():
            return ExplorerTurnAdvance(handled=True, progressed=progressed)

        context = self.host.current_turn_context()
        handoff = self.service.create_handoff(
            context.turn_id,
            root_candidate=context.root_candidate,
            id_factory=self.collaborator.handoff_id_for,
        )
        receipt = self.collaborator.accept_explorer_handoff(handoff)
        return ExplorerTurnAdvance(
            handled=True,
            progressed=True,
            handoff=handoff,
            collaborator_receipt=receipt,
        )


__all__ = ["ExplorerProgram", "ExplorerProgramError", "ExplorerTurnAdvance"]
