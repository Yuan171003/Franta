from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from franta.explorer_control import state as explorer_state
from franta.phase_control import state as phase_state


UTC = timezone.utc
T0 = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


class PhaseControlTests(unittest.TestCase):
    def test_disabled_install_has_no_legacy_state_effect(self) -> None:
        legacy = {"gate": "open", "tasks": {}, "events": [{"event_id": 1}]}
        original = copy.deepcopy(legacy)
        transition = phase_state.install_phase_control(
            legacy, enabled=False, now=lambda: T0
        )
        self.assertFalse(transition.changed)
        self.assertEqual(transition.state, original)
        self.assertEqual(legacy, original)
        self.assertNotIn(phase_state.PHASE_CONTROL_KEY, transition.state)

    def test_reducers_are_pure_and_explorer_deadline_is_inclusive(self) -> None:
        initial = phase_state.initialize_phase_state(now=lambda: T0)
        original = copy.deepcopy(initial)
        before = phase_state.tick(
            initial,
            now=T0 + phase_state.EXPLORER_ADMISSION_DURATION - timedelta(microseconds=1),
        )
        self.assertFalse(before.changed)
        self.assertEqual(before.state["phase"], "explorer_admission")
        at = phase_state.tick(
            initial, now=T0 + phase_state.EXPLORER_ADMISSION_DURATION
        )
        self.assertTrue(at.changed)
        self.assertEqual(at.state["phase"], "explorer_drain")
        self.assertEqual(at.state["explorer"]["drain_reason"], "deadline")
        self.assertEqual(initial, original)

    def test_sort_barrier_binds_same_main_session_then_starts_eight_hours(self) -> None:
        control = phase_state.initialize_phase_state(now=T0)
        control = phase_state.request_explorer_drain(
            control, reason="deadline", now=T0 + timedelta(hours=6)
        ).state
        control = phase_state.begin_franta_sort(
            control,
            explorer_drained=True,
            sort_id="SORT-1",
            sort_call_id="CALL-SORT-1",
            main_session_key="main:cycle:1",
            now=T0 + timedelta(hours=7),
        ).state
        with self.assertRaises(phase_state.PhaseConflictError):
            phase_state.complete_sort_barrier(
                control,
                sort_call_id="CALL-SORT-1",
                planning_call_id="CALL-MAIN-1",
                main_session_key="main:other",
                now=T0 + timedelta(hours=8),
            )
        opened = phase_state.complete_sort_barrier(
            control,
            sort_call_id="CALL-SORT-1",
            planning_call_id="CALL-MAIN-1",
            main_session_key="main:cycle:1",
            now=T0 + timedelta(hours=8),
        )
        self.assertEqual(opened.state["phase"], "franta_run")
        self.assertEqual(
            opened.state["sort"]["main_session_key"], "main:cycle:1"
        )
        deadline = datetime.fromisoformat(
            opened.state["franta"]["admission_deadline"]
        )
        self.assertEqual(deadline, T0 + timedelta(hours=16))
        drained = phase_state.tick(opened.state, now=deadline)
        self.assertEqual(drained.state["phase"], "franta_drain")
        next_turn = phase_state.complete_franta_drain(
            drained.state, franta_drained=True, now=deadline + timedelta(hours=1)
        )
        self.assertEqual(next_turn.state["phase"], "explorer_admission")
        self.assertEqual(next_turn.state["cycle"], 2)
        self.assertEqual(len(next_turn.state["history"]), 1)

    def test_phase_state_recovers_from_json_and_expires_without_prior_tick(self) -> None:
        durable = json.loads(
            json.dumps(phase_state.initialize_phase_state(now=T0))
        )
        recovered = phase_state.tick(durable, now=T0 + timedelta(days=1))
        self.assertEqual(recovered.state["phase"], "explorer_drain")
        self.assertEqual(recovered.state["explorer"]["drain_reason"], "deadline")

    def test_phase_durations_are_persisted_and_replay_checked(self) -> None:
        installed = phase_state.install_phase_control(
            {"legacy": True},
            enabled=True,
            now=T0,
            explorer_admission_seconds=7,
            franta_admission_seconds=11,
        )
        control = installed.state[phase_state.PHASE_CONTROL_KEY]
        self.assertEqual(
            control["settings"],
            {
                "explorer_admission_seconds": 7,
                "franta_admission_seconds": 11,
            },
        )
        self.assertEqual(
            datetime.fromisoformat(control["explorer"]["admission_deadline"]),
            T0 + timedelta(seconds=7),
        )
        replay = phase_state.install_phase_control(
            installed.state,
            enabled=True,
            now=T0 + timedelta(days=1),
            explorer_admission_seconds=7,
            franta_admission_seconds=11,
        )
        self.assertFalse(replay.changed)
        with self.assertRaises(phase_state.PhaseConflictError):
            phase_state.install_phase_control(
                installed.state,
                enabled=True,
                now=T0,
                explorer_admission_seconds=8,
                franta_admission_seconds=11,
            )
        control = phase_state.tick(control, now=T0 + timedelta(seconds=7)).state
        control = phase_state.begin_franta_sort(
            control,
            explorer_drained=True,
            sort_id="SORT-CUSTOM",
            sort_call_id="SORT-CALL-CUSTOM",
            main_session_key="main:custom",
            now=T0 + timedelta(seconds=8),
        ).state
        control = phase_state.complete_sort_barrier(
            control,
            sort_call_id="SORT-CALL-CUSTOM",
            planning_call_id="PLAN-CALL-CUSTOM",
            main_session_key="main:custom",
            now=T0 + timedelta(seconds=9),
        ).state
        franta_deadline = datetime.fromisoformat(
            control["franta"]["admission_deadline"]
        )
        self.assertEqual(franta_deadline, T0 + timedelta(seconds=20))
        control = phase_state.tick(control, now=franta_deadline).state
        next_turn = phase_state.complete_franta_drain(
            control, franta_drained=True, now=franta_deadline + timedelta(seconds=1)
        ).state
        self.assertEqual(
            datetime.fromisoformat(next_turn["explorer"]["admission_deadline"]),
            franta_deadline + timedelta(seconds=8),
        )


class ExplorerLineageTests(unittest.TestCase):
    def _admit(self, state, lineage_id, *, now=T0, max_slots=4):
        return explorer_state.admit_lineage(
            state,
            lineage_id=lineage_id,
            session_key=f"explorer:{lineage_id}",
            phase_cycle=1,
            phase_epoch=1,
            admission_allowed=True,
            now=now,
            max_slots=max_slots,
        ).state

    def test_dynamic_slots_have_no_direction_serial_gate(self) -> None:
        state = explorer_state.initialize_explorer_state()
        original = copy.deepcopy(state)
        for number in range(1, 5):
            state = self._admit(state, f"L-{number}")
        self.assertEqual(explorer_state.available_slots(state), 0)
        self.assertEqual(len(explorer_state.active_lineage_ids(state)), 4)
        self.assertFalse(any("direction" in item for item in state["lineages"].values()))
        with self.assertRaises(explorer_state.ExplorerCapacityError):
            self._admit(state, "L-5")
        self.assertEqual(original, explorer_state.initialize_explorer_state())

    def test_admitted_before_deadline_may_finish_exactly_three_attempts(self) -> None:
        control = phase_state.initialize_phase_state(now=T0)
        state = self._admit(explorer_state.initialize_explorer_state(), "L-1")
        # The admission window closes, but the already admitted lineage remains
        # independently eligible for its full attempt allowance.
        control = phase_state.tick(control, now=T0 + timedelta(hours=6)).state
        self.assertEqual(control["phase"], "explorer_drain")
        for number in range(1, 4):
            started = explorer_state.start_attempt(
                state,
                lineage_id="L-1",
                call_id=f"CALL-{number}",
                now=T0 + timedelta(hours=6 + 4 * (number - 1)),
            )
            state = explorer_state.acknowledge_attempt_end(
                started.state,
                lineage_id="L-1",
                call_id=f"CALL-{number}",
                outcome="progress",
                now=T0 + timedelta(hours=6 + 4 * number),
            ).state
        lineage = state["lineages"]["L-1"]
        self.assertEqual(lineage["attempts_started"], 3)
        self.assertEqual(lineage["status"], "closed")
        self.assertFalse(lineage["slot_reserved"])
        self.assertTrue(explorer_state.drained(state))
        with self.assertRaises(explorer_state.ExplorerStateError):
            explorer_state.start_attempt(
                state, lineage_id="L-1", call_id="CALL-4", now=T0 + timedelta(hours=19)
            )

    def test_four_hour_watchdog_requests_stop_then_continues(self) -> None:
        state = self._admit(explorer_state.initialize_explorer_state(), "L-1")
        state = explorer_state.start_attempt(
            state, lineage_id="L-1", call_id="CALL-1", now=T0
        ).state
        before = explorer_state.request_expired_attempt_stops(
            state, now=T0 + timedelta(hours=4) - timedelta(microseconds=1)
        )
        self.assertFalse(before.changed)
        expired = explorer_state.request_expired_attempt_stops(
            state, now=T0 + timedelta(hours=4)
        )
        self.assertEqual(expired.cancel_call_ids, ("CALL-1",))
        self.assertEqual(
            expired.state["lineages"]["L-1"]["status"], "stop_requested"
        )
        ended = explorer_state.acknowledge_attempt_end(
            expired.state,
            lineage_id="L-1",
            call_id="CALL-1",
            outcome="timed_out",
            now=T0 + timedelta(hours=4, minutes=1),
        )
        self.assertEqual(
            ended.state["lineages"]["L-1"]["status"], "continuation_pending"
        )

    def test_root_candidate_is_unverified_and_immediately_stops_all(self) -> None:
        state = explorer_state.initialize_explorer_state()
        state = self._admit(state, "L-1")
        state = self._admit(state, "L-2")
        state = self._admit(state, "L-3")
        state = explorer_state.start_attempt(
            state, lineage_id="L-1", call_id="CALL-1", now=T0
        ).state
        state = explorer_state.start_attempt(
            state, lineage_id="L-2", call_id="CALL-2", now=T0
        ).state
        claimed = explorer_state.claim_root_candidate(
            state,
            candidate_id="RC-1",
            scratch_id="S-ROOT-1",
            lineage_id="L-1",
            call_id="CALL-1",
            candidate_outcome="proved",
            now=T0 + timedelta(hours=1),
        )
        self.assertEqual(claimed.cancel_call_ids, ("CALL-1", "CALL-2"))
        self.assertEqual(claimed.state["root_candidate"]["status"], "unverified_candidate")
        self.assertNotIn("canonical_id", claimed.state["root_candidate"])
        self.assertEqual(claimed.state["lineages"]["L-3"]["status"], "stopped")

        control = phase_state.initialize_phase_state(now=T0)
        phase_claim = phase_state.accept_root_candidate(
            control,
            candidate_id="RC-1",
            scratch_id="S-ROOT-1",
            lineage_id="L-1",
            attempt_number=1,
            candidate_outcome="proved",
            now=T0 + timedelta(hours=1),
        )
        self.assertEqual(phase_claim.state["phase"], "explorer_drain")
        self.assertEqual(
            phase_claim.state["explorer"]["root_candidate"]["status"],
            "unverified_candidate",
        )

    def test_restart_fences_lost_call_without_exceeding_attempt_budget(self) -> None:
        state = self._admit(explorer_state.initialize_explorer_state(), "L-1")
        state = explorer_state.start_attempt(
            state, lineage_id="L-1", call_id="CALL-1", now=T0
        ).state
        durable = json.loads(json.dumps(state))
        recovered = explorer_state.reconcile_after_restart(
            durable, live_call_ids=(), now=T0 + timedelta(hours=1)
        )
        self.assertEqual(
            recovered.state["lineages"]["L-1"]["status"],
            "continuation_pending",
        )
        state = recovered.state
        for number in (2, 3):
            state = explorer_state.start_attempt(
                state, lineage_id="L-1", call_id=f"CALL-{number}", now=T0
            ).state
            state = explorer_state.acknowledge_attempt_end(
                state,
                lineage_id="L-1",
                call_id=f"CALL-{number}",
                outcome="progress",
                now=T0,
            ).state
        self.assertEqual(state["lineages"]["L-1"]["attempts_started"], 3)
        self.assertEqual(state["lineages"]["L-1"]["status"], "closed")

    def test_attempt_settings_are_persisted_and_bound_to_each_lineage(self) -> None:
        state = explorer_state.initialize_explorer_state(
            attempt_limit=2, attempt_seconds=17
        )
        state = self._admit(state, "L-1")
        self.assertEqual(
            state["settings"], {"attempt_limit": 2, "attempt_seconds": 17}
        )
        with self.assertRaises(explorer_state.ExplorerConflictError):
            explorer_state.start_attempt(
                state,
                lineage_id="L-1",
                call_id="CALL-BAD-CONFIG",
                now=T0,
            )
        started = explorer_state.start_attempt(
            state,
            lineage_id="L-1",
            call_id="CALL-1",
            now=T0,
            attempt_limit=2,
            attempt_seconds=17,
        )
        deadline = datetime.fromisoformat(
            started.state["lineages"]["L-1"]["attempts"][0]["deadline"]
        )
        self.assertEqual(deadline, T0 + timedelta(seconds=17))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
