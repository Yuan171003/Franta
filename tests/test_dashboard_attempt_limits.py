from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from franta.attempt_budgets import apply_pending, install, queue_limits
from franta.dashboard_adapter import FrantaOperatorCommands
from franta.dashboard_commands import (
    consume_attempt_limit_commands, latest_pending_attempt_limits,
    pending_attempt_limit_commands, submit_attempt_limit_command,
)
from franta.dashboard_read import FrantaDashboardRead
from franta.store import MemoryStore


class DashboardAttemptLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MemoryStore(self.root / "scheduler.sqlite3", projection_dir=False)
        self.addCleanup(self.store.close)
        self.state = {
            "root": {"problem": "Prove ROOT."}, "tasks": {}, "calls": {}, "gate": "open",
            "phase_control": {"enabled": True, "phase": "explorer_admission", "cycle": 1, "explorer": {}, "franta": {}},
        }
        install(self.state, explorer_limit=20, franta_limit=30)
        self.save()
        self.queued = []
        self.runtime = SimpleNamespace(layout=SimpleNamespace(root=self.root), scheduler=SimpleNamespace(queue_attempt_limits=self.queue))
        self.read = FrantaDashboardRead(self.root)
        self.commands = FrantaOperatorCommands(self.root, self.read)
        self.now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)

    def save(self) -> None:
        self.store.save_control_state("scheduler.v1", self.state)

    def queue(self, **command) -> bool:
        self.queued.append(command)
        changed = queue_limits(self.state, **command)
        if changed:
            self.save()
        return changed

    def submit(self, explorer: int, franta: int, at: datetime) -> dict:
        with patch("franta.dashboard_commands.datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            return self.commands.submit_attempt_limits(explorer, franta)

    def test_server_submission_is_durable_and_read_only_until_runner_consumes(self) -> None:
        original = self.store.load_control_state("scheduler.v1")
        saved = self.submit(25, 35, self.now)
        self.assertEqual(self.store.load_control_state("scheduler.v1"), original)
        # Reconstructing the reader/command adapter models a dashboard restart.
        reopened = FrantaDashboardRead(self.root).overview()["attempt_budgets"]
        self.assertEqual(reopened["explorer"]["limit"], 20)
        self.assertEqual(reopened["pending"]["explorer_limit"], 25)
        self.assertEqual(datetime.fromisoformat(reopened["pending"]["effective_at"]), self.now + timedelta(seconds=120))
        self.assertTrue(consume_attempt_limit_commands(self.runtime))
        self.assertFalse(pending_attempt_limit_commands(self.root))
        self.assertEqual(self.read.overview()["attempt_budgets"]["pending"]["command_id"], saved["command_id"])
        self.assertIsNone(apply_pending(self.state, now=self.now + timedelta(seconds=119)))
        apply_pending(self.state, now=self.now + timedelta(seconds=120))
        self.save()
        applied = FrantaDashboardRead(self.root).overview()["attempt_budgets"]
        self.assertEqual((applied["explorer"]["limit"], applied["franta"]["limit"]), (25, 35))
        self.assertIsNone(applied["pending"])

    def test_only_latest_edit_is_queued_and_restarts_grace_period(self) -> None:
        older = self.submit(2, 3, self.now)
        newest = self.submit(40, 50, self.now + timedelta(seconds=119))
        consume_attempt_limit_commands(self.runtime)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0]["command_id"], newest["command_id"])
        self.assertIsNone(apply_pending(self.state, now=self.now + timedelta(seconds=120)))
        self.assertIsNotNone(apply_pending(self.state, now=self.now + timedelta(seconds=239)))
        receipt = json.loads((self.root / "private/dashboard/receipts" / (older["command_id"] + ".json")).read_text())
        self.assertEqual(receipt["status"], "superseded")
        self.assertEqual(receipt["superseded_by"], newest["command_id"])

    def test_same_clock_tick_still_preserves_submission_order(self) -> None:
        self.submit(2, 3, self.now)
        newest = self.submit(40, 50, self.now)
        self.assertEqual(latest_pending_attempt_limits(self.root)["command_id"], newest["command_id"])
        self.assertGreater(datetime.fromisoformat(newest["created_at"]), self.now)

    def test_new_unconsumed_edit_replaces_accepted_pending_projection(self) -> None:
        self.submit(25, 35, self.now)
        consume_attempt_limit_commands(self.runtime)
        newest = self.submit(28, 38, self.now + timedelta(seconds=60))
        budgets = self.read.overview()["attempt_budgets"]
        self.assertEqual(budgets["explorer"]["limit"], 20)
        self.assertEqual(budgets["pending"]["command_id"], newest["command_id"])
        self.assertEqual(budgets["pending"]["explorer_limit"], 28)

    def test_missing_receipt_replay_does_not_restart_delay_or_show_stale_edit(self) -> None:
        older = self.submit(25, 35, self.now)
        consume_attempt_limit_commands(self.runtime)
        newer = self.submit(28, 38, self.now + timedelta(seconds=60))
        consume_attempt_limit_commands(self.runtime)
        apply_pending(self.state, now=self.now + timedelta(seconds=180))
        self.save()
        receipts = self.root / "private/dashboard/receipts"
        (receipts / (older["command_id"] + ".json")).unlink()
        self.assertIsNone(self.read.overview()["attempt_budgets"]["pending"])
        self.assertFalse(consume_attempt_limit_commands(self.runtime))
        (receipts / (newer["command_id"] + ".json")).unlink()
        self.assertFalse(consume_attempt_limit_commands(self.runtime))
        self.assertIsNone(self.read.overview()["attempt_budgets"]["pending"])

    def test_corrupt_command_is_rejected_without_displacing_valid_edit(self) -> None:
        newest = self.submit(28, 38, self.now)
        corrupt = self.root / "private/dashboard/commands/ATL-corrupt.json"
        corrupt.write_text("[]")
        consume_attempt_limit_commands(self.runtime)
        self.assertEqual(self.queued[0]["command_id"], newest["command_id"])
        receipt = json.loads((self.root / "private/dashboard/receipts/ATL-corrupt.json").read_text())
        self.assertEqual(receipt["status"], "rejected")

    def test_direct_submission_rejects_non_integer_limits(self) -> None:
        for invalid in (0, -1, True, 2.5, "20", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                submit_attempt_limit_command(self.root, invalid, 30)

    def test_legacy_read_projects_defaults_without_rewriting_state(self) -> None:
        self.state["phase_control"].pop("attempt_budget")
        self.save()
        original = self.store.load_control_state("scheduler.v1")
        budgets = self.read.overview()["attempt_budgets"]
        self.assertEqual((budgets["explorer"]["limit"], budgets["franta"]["limit"]), (20, 30))
        self.assertFalse(budgets["enabled"])
        self.assertTrue(budgets["available"])
        self.assertEqual(self.store.load_control_state("scheduler.v1"), original)

    def test_non_explorer_project_has_no_controls_and_rejects_edits_before_writing(self) -> None:
        self.state.pop("phase_control")
        self.save()
        budgets = self.read.overview()["attempt_budgets"]
        self.assertFalse(budgets["available"])
        with self.assertRaisesRegex(ValueError, "Explorer phases"):
            self.commands.submit_attempt_limits(25, 35)
        self.assertFalse((self.root / "private/dashboard/commands").exists())

    def test_legacy_explorer_can_queue_limits_for_resume_migration(self) -> None:
        self.state["phase_control"].pop("attempt_budget")
        self.save()
        saved = self.submit(25, 35, self.now)
        budgets = self.read.overview()["attempt_budgets"]
        self.assertTrue(budgets["available"])
        self.assertFalse(budgets["enabled"])
        self.assertEqual(budgets["pending"]["command_id"], saved["command_id"])


if __name__ == "__main__":
    unittest.main()
