from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from franta.contracts.configuration import ExplorerSettings as FrantaExplorerSettings
from franta.contracts.failures import ConfigurationError
from franta.execution_gateway.skills import (
    SKILLS,
    SkillContext,
    SkillRuntime,
    SkillRuntimeError,
    _tool_definitions,
    allowed_skills,
)
from franta.access import policy_for
from explorer_system.settings import (
    ExplorerSettings,
    ExplorerSettingsValidationError,
)
from explorer_system import alternation
from explorer_system import contracts as portable_contracts
from explorer_system.tools import (
    EXPLORER_SKILLS,
    ExplorerToolValidationError,
    explorer_tool_definitions,
    explorer_worker_skills,
    normalize_scratch_payload,
    normalize_summary_payload,
)


class PortableExplorerSettingsTests(unittest.TestCase):
    def test_settings_are_valid_without_importing_a_host(self) -> None:
        settings = ExplorerSettings()
        settings.validate()
        self.assertEqual(settings.attempts_per_worker, 3)
        self.assertEqual(settings.attempt_seconds, 4 * 60 * 60)
        self.assertEqual(settings.explorer_admission_seconds, 2 * 60 * 60)

        with self.assertRaisesRegex(
            ExplorerSettingsValidationError,
            "attempt_seconds cannot exceed 14400 seconds",
        ):
            ExplorerSettings(attempt_seconds=4 * 60 * 60 + 1).validate()

        with self.assertRaisesRegex(
            ExplorerSettingsValidationError,
            "max_scratch_per_turn must be at least",
        ):
            ExplorerSettings(
                max_scratch_per_attempt=3,
                max_scratch_per_turn=2,
            ).validate()

    def test_franta_compatibility_type_preserves_configuration_error(self) -> None:
        self.assertFalse(issubclass(FrantaExplorerSettings, ExplorerSettings))
        self.assertEqual(
            FrantaExplorerSettings().franta_admission_seconds,
            ExplorerSettings().host_admission_seconds,
        )
        with self.assertRaisesRegex(
            ConfigurationError,
            "remove the table to retain legacy Franta scheduling",
        ):
            FrantaExplorerSettings(enabled=False).validate()

    def test_portable_alternation_persists_only_host_neutral_wire(self) -> None:
        started_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
        state = alternation.initialize_phase_state(
            now=started_at,
            explorer_admission_seconds=7,
            host_admission_seconds=11,
        )
        self.assertEqual(
            state["settings"],
            {
                "explorer_admission_seconds": 7,
                "host_admission_seconds": 11,
            },
        )
        self.assertIn("host", state)
        self.assertNotIn("franta", json.dumps(state).lower())

        state = alternation.request_explorer_drain(
            state, reason="deadline", now=started_at + timedelta(seconds=7)
        ).state
        sort = alternation.begin_host_sort(
            state,
            explorer_drained=True,
            sort_id="sort-1",
            sort_call_id="call-sort-1",
            main_session_key="host-session-1",
            now=started_at + timedelta(seconds=8),
        )
        self.assertEqual(sort.state["phase"], "host_sort")
        self.assertEqual(sort.events[0]["type"], "host_sort_started")
        opened = alternation.complete_sort_barrier(
            sort.state,
            sort_call_id="call-sort-1",
            planning_call_id="call-plan-1",
            main_session_key="host-session-1",
            now=started_at + timedelta(seconds=9),
        )
        self.assertEqual(opened.state["phase"], "host_run")
        self.assertEqual(opened.events[0]["type"], "host_sort_barrier_opened")
        deadline = datetime.fromisoformat(opened.state["host"]["admission_deadline"])
        drained = alternation.tick(opened.state, now=deadline)
        self.assertEqual(drained.state["phase"], "host_drain")
        self.assertEqual(drained.events[0]["type"], "host_admission_closed")
        next_turn = alternation.complete_host_drain(
            drained.state,
            host_drained=True,
            now=deadline + timedelta(seconds=1),
        )
        self.assertEqual(next_turn.state["phase"], "explorer_admission")
        self.assertNotIn("franta", json.dumps(next_turn.state).lower())
        self.assertFalse(hasattr(alternation.Phase, "FRANTA_RUN"))

    def test_portable_contract_has_no_franta_export_vocabulary(self) -> None:
        self.assertFalse(hasattr(portable_contracts, "EXPLORER_PROMOTION_KINDS"))
        self.assertFalse(hasattr(portable_contracts, "ExplorerPromotion"))


class PortableExplorerToolTests(unittest.TestCase):
    @staticmethod
    def _ids(prefix: str) -> str:
        return f"{prefix}-fixed"

    def test_normalizes_scratch_and_summary_without_host_runtime(self) -> None:
        scratch = normalize_scratch_payload(
            {
                "operation_id": "OP-1",
                "record_kind": "route",
                "abstract": "  Route  ",
                "content": "  Try valuations.  ",
                "related_memory_ids": ["F-1"],
            },
            new_record_id=self._ids,
        )
        self.assertEqual(scratch["record_id"], "ES-fixed")
        self.assertEqual(scratch["abstract"], "Route")
        self.assertNotIn("ongoing_direction", scratch)
        self.assertNotIn("previous_direction_id", scratch)

        summary = normalize_summary_payload(
            {
                "abstract": " Summary ",
                "content": " Details ",
                "directions_tried": [" route A "],
                "main_progress": " progress ",
                "main_obstacles": " obstacle ",
                "source_scratch_ids": [scratch["record_id"]],
            },
            new_record_id=self._ids,
        )
        self.assertEqual(summary["record_id"], "ESUM-fixed")
        self.assertEqual(summary["directions_tried"], ["route A"])

    def test_direction_payload_is_retired(self) -> None:
        with self.assertRaisesRegex(
            ExplorerToolValidationError,
            "unsupported fields: ongoing_direction",
        ):
            normalize_scratch_payload(
                {
                    "abstract": "Idea",
                    "content": "Content",
                    "ongoing_direction": True,
                }
            )
        with self.assertRaisesRegex(
            ExplorerToolValidationError,
            "invalid record_kind",
        ):
            normalize_scratch_payload(
                {
                    "record_kind": "direction",
                    "abstract": "Direction",
                    "content": "Retired direction record.",
                }
            )
    def test_summary_still_requires_a_source_scratch(self) -> None:
        with self.assertRaisesRegex(
            ExplorerToolValidationError,
            "source_scratch_ids must cite at least one",
        ):
            normalize_summary_payload(
                {
                    "abstract": "Summary",
                    "content": "Content",
                    "directions_tried": ["route"],
                    "main_progress": "progress",
                    "main_obstacles": "obstacle",
                    "source_scratch_ids": [],
                }
            )

    def test_franta_delegates_skill_set_and_mcp_definitions(self) -> None:
        self.assertEqual(SKILLS[-len(EXPLORER_SKILLS) :], EXPLORER_SKILLS)
        expected = explorer_worker_skills(search_enabled=True) | {"CAS"}
        self.assertEqual(
            allowed_skills(policy_for("explorer-worker", mode="explore")),
            expected,
        )
        portable = explorer_tool_definitions(
            {"fact", "memo"},
            host_name="SampleHost",
        )
        self.assertIn(
            "published SampleHost summaries",
            portable["explorer_search"]["description"],
        )

        enabled = portable.keys()
        franta = {item["name"]: item for item in _tool_definitions(enabled)}
        self.assertEqual(set(franta), set(portable))
        self.assertIn(
            "published Franta summaries",
            franta["explorer_search"]["description"],
        )

    def test_franta_maps_portable_validation_errors_to_legacy_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "outbox").mkdir()
            runtime = SkillRuntime(
                SkillContext(
                    workspace=workspace,
                    policy=policy_for("explorer-worker", mode="clean-room"),
                    attempt=1,
                )
            )
            with self.assertRaisesRegex(
                SkillRuntimeError,
                "record-scratch has an invalid record_kind",
            ):
                runtime.invoke(
                    "record-scratch",
                    {
                        "record_kind": "invalid",
                        "abstract": "Idea",
                        "content": "Content",
                    },
                )


if __name__ == "__main__":
    unittest.main()
