from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from datetime import datetime, timezone

from franta.contracts.agent_access import MemoryRecord, policy_for
from franta.explorer.contracts import ExplorerReadScope
from franta.explorer.repository import ExplorerRepository
from franta.explorer_adapter import (
    AuditedExplorerAPI,
    FrantaExplorerHost,
    explorer_api_for_runtime_call,
)
from franta.execution_gateway.broker import MemoryBroker
from franta.execution_gateway.skills import allowed_skills
from franta.read_access.memory import AccessError, InMemoryBackend
from franta.scheduler import Scheduler
from franta.testing import FakeControlStore
from explorer_system.contracts import (
    ExplorerPublishedMemoryDocument,
    ExplorerPublishedMemorySnapshot,
)
from explorer_system.service import ExplorerAttempt, ExplorerService


class _GrantAPI:
    def check_result(self, kind: str, statement: str):
        return [{"status": "established", "abstract": statement}]

    def portfolio_search(self, query: str, *, limit: int = 10):
        return [{"portfolio_item_id": "XPI-one", "abstract": query}][:limit]

    def portfolio_fetch(self, portfolio_item_id: str):
        return {"portfolio_item_id": portfolio_item_id, "abstract": "item"}

    def search(self, query, record_types, **kwargs):
        return [{"id": "ES-one", "abstract": query}]

    def fetch(self, record_id):
        return {"id": record_id}


def _v2_policy(mode: str):
    return replace(
        policy_for("explorer-worker", mode=mode),
        explorer_access_grant_id="XAG-test",
        explorer_access_grant_digest="a" * 64,
    )


class ExplorerAttemptAccessIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.broker = MemoryBroker(InMemoryBackend())
        self.broker.start(Path(self.temporary.name) / "broker.sock")

    def tearDown(self) -> None:
        self.broker.stop()
        self.temporary.cleanup()

    def _binding(self, mode: str):
        return self.broker.issue(
            _v2_policy(mode), caller_id=f"call-{mode}", explorer_api=_GrantAPI()
        )

    def _dispatch(self, binding, operation: str, arguments=None):
        return self.broker.dispatch(
            {
                "token": binding.token,
                "operation": operation,
                "arguments": arguments or {},
            }
        )

    def test_attempt_one_has_only_check_result_memory_capability(self) -> None:
        binding = self._binding("check-result")
        self.assertEqual(binding.enabled_tools, ("check_result",))
        result = self._dispatch(
            binding,
            "check_result",
            {"kind": "proved", "statement": "Every group G is isomorphic to G."},
        )
        self.assertEqual(result[0]["status"], "established")
        for forbidden in (
            "internal_search",
            "memory_fetch",
            "portfolio_search",
            "portfolio_fetch",
            "explorer_search",
            "explorer_fetch",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(AccessError):
                self._dispatch(binding, forbidden)

    def test_attempt_two_is_confined_to_check_and_frozen_portfolio(self) -> None:
        binding = self._binding("portfolio")
        self.assertEqual(
            set(binding.enabled_tools),
            {"check_result", "portfolio_search", "portfolio_fetch"},
        )
        self.assertEqual(
            self._dispatch(
                binding, "portfolio_search", {"query": "groups", "limit": 1}
            )[0]["portfolio_item_id"],
            "XPI-one",
        )
        for forbidden in (
            "internal_search",
            "memory_fetch",
            "explorer_search",
            "explorer_fetch",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(AccessError):
                self._dispatch(binding, forbidden)

    def test_attempt_three_has_only_joint_full_memory_search(self) -> None:
        binding = self._binding("full-memory")
        self.assertEqual(
            set(binding.enabled_tools), {"explorer_search", "explorer_fetch"}
        )
        for forbidden in (
            "internal_search",
            "memory_fetch",
            "check_result",
            "portfolio_search",
            "portfolio_fetch",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaises(AccessError):
                self._dispatch(binding, forbidden)

    def test_attempt_three_joint_search_can_read_canonical_memory(self) -> None:
        repository = ExplorerRepository(Path(self.temporary.name) / "explorer.sqlite3")
        try:
            policy = _v2_policy("full-memory")
            api = AuditedExplorerAPI(
                InMemoryBackend(
                    [
                        MemoryRecord(
                            "F-group",
                            "fact",
                            "Every group is isomorphic to itself.",
                            "Proof by the identity morphism.",
                        )
                    ]
                ),
                repository,
                policy,
                ExplorerReadScope(
                    allowed_turn_ids=frozenset(), max_seq=0, label="attempt-3"
                ),
            )
            result = api.search("group isomorphic", ["fact"])
            self.assertEqual(result[0]["id"], "F-group")
        finally:
            repository.close()

    def test_attempt_three_replays_frozen_host_snapshot_and_validates_grant(self) -> None:
        repository = ExplorerRepository(Path(self.temporary.name) / "frozen.sqlite3")
        try:
            service = ExplorerService(repository)
            original = MemoryRecord(
                "F-original",
                "fact",
                "Original spectral invariant",
                "The original spectral invariant is zero.",
                metadata={"statement": "The original spectral invariant is zero."},
            )
            snapshot = ExplorerPublishedMemorySnapshot(
                revision="host-revision-1",
                documents=(
                    ExplorerPublishedMemoryDocument(
                        source_id=original.memory_id,
                        memory_kind=original.memory_type,
                        abstract=original.abstract,
                        main_content=original.content,
                        full_record_json=json.dumps(
                            original.full(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
            )
            attempt = ExplorerAttempt("turn-frozen", "worker-frozen", 3, "call-frozen")
            grant = service.create_attempt_access_grant(
                attempt, host_snapshot=snapshot
            )
            drifted = InMemoryBackend(
                [
                    MemoryRecord(
                        "F-drifted",
                        "fact",
                        "Later spectral invariant",
                        "The later spectral invariant is one.",
                    )
                ]
            )

            class _Store:
                def as_memory_backend(self):
                    return drifted

            policy = replace(
                policy_for("explorer-worker", mode="full-memory"),
                explorer_access_grant_id=grant.grant_id,
                explorer_access_grant_digest=grant.input_digest,
            )
            payload = {
                "access_policy_version": 2,
                "access_mode": "full-memory",
                "grant_id": grant.grant_id,
                "grant_digest": grant.input_digest,
                "explorer_turn_id": attempt.turn_id,
                "worker_session_id": attempt.worker_session_id,
                "attempt_number": attempt.attempt_no,
                "source_high_water_seq": 0,
            }
            runtime = SimpleNamespace(
                explorer_service=service,
                explorer_repository=repository,
                read_audit=None,
                store=_Store(),
            )
            call = SimpleNamespace(
                payload=payload,
                policy=policy,
                kind="explorer-worker",
                call_id=attempt.call_id,
            )
            api = explorer_api_for_runtime_call(runtime, call)
            self.assertEqual(
                api.search("original spectral", ["fact"])[0]["id"],
                "F-original",
            )
            self.assertEqual(api.search("later", ["fact"]), [])
            self.assertEqual(api.fetch("F-original")["id"], "F-original")
            with self.assertRaises(AccessError):
                api.fetch("F-drifted")

            mismatched = SimpleNamespace(
                payload={**payload, "grant_digest": "0" * 64},
                policy=policy,
                kind="explorer-worker",
                call_id=attempt.call_id,
            )
            with self.assertRaisesRegex(RuntimeError, "does not match its call"):
                explorer_api_for_runtime_call(runtime, mismatched)
        finally:
            repository.close()

    def test_franta_snapshot_includes_obligations_for_attempt_three(self) -> None:
        obligation = MemoryRecord(
            "O-root-step",
            "obligation",
            "Control the boundary morphism",
            "Prove that the boundary morphism vanishes.",
            metadata={"statement": "The boundary morphism is zero."},
        )

        class _Store:
            def as_memory_backend(self):
                return InMemoryBackend([obligation])

        host = FrantaExplorerHost(SimpleNamespace(store=_Store()))
        snapshot = host.explorer_memory_snapshot()
        self.assertEqual(
            [(item.source_id, item.memory_kind) for item in snapshot.documents],
            [("O-root-step", "obligation")],
        )
        self.assertIsNotNone(snapshot.documents[0].full_record_json)

    def test_skill_sets_match_the_three_mechanical_modes(self) -> None:
        self.assertEqual(
            allowed_skills(_v2_policy("check-result")),
            {"check-result", "record-scratch", "record-summary", "CAS"},
        )
        self.assertEqual(
            allowed_skills(_v2_policy("portfolio")),
            {
                "check-result",
                "portfolio-search",
                "record-scratch",
                "record-summary",
                "CAS",
            },
        )
        self.assertEqual(
            allowed_skills(_v2_policy("full-memory")),
            {"explorer-search", "record-scratch", "record-summary", "CAS"},
        )

    def test_v1_recovery_policies_keep_the_old_wire_shape(self) -> None:
        clean = policy_for("explorer-worker", mode="clean-room")
        explore = policy_for("explorer-worker", mode="explore")
        for public in (clean.as_public_dict(), explore.as_public_dict()):
            self.assertNotIn("explorer_access_policy_version", public)
            self.assertNotIn("explorer_access_mode", public)
            self.assertNotIn("explorer_access_grant_id", public)
            self.assertNotIn("explorer_access_grant_digest", public)
        self.assertEqual(
            allowed_skills(clean),
            {"record-scratch", "record-summary", "CAS"},
        )
        self.assertEqual(
            allowed_skills(explore),
            {"explorer-search", "record-scratch", "record-summary", "CAS"},
        )

    def test_new_call_persists_complete_v2_grant_while_old_call_stays_v1(self) -> None:
        at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        control_store = FakeControlStore()
        scheduler = Scheduler(control_store)
        scheduler.bootstrap(root_problem="Prove ROOT")
        scheduler.commit_initial_trim({"category_ids": []})
        scheduler.configure_alternation(
            {
                "attempts_per_worker": 3,
                "attempt_seconds": 10_800,
                "explorer_admission_seconds": 43_200,
                "franta_admission_seconds": 54_000,
            },
            now=at,
        )
        lineage = scheduler.admit_explorer_lineage(now=at)
        metadata = {
            "access_policy_version": 2,
            "access_mode": "check-result",
            "grant_id": "XAG-persisted",
            "grant_digest": "b" * 64,
        }
        prepared = scheduler.start_explorer_attempt(
            lineage,
            source_high_water_seq=0,
            guidance_variant="check-result-uncommon",
            access_grant=metadata,
            now=at,
        )
        persisted = scheduler.state["calls"][prepared["call_id"]]["input"]
        self.assertEqual(
            {key: persisted[key] for key in metadata}, metadata
        )
        self.assertEqual(
            prepared["guidance_variant"], "check-result-uncommon"
        )
        self.assertEqual(
            persisted["guidance_variant"], "check-result-uncommon"
        )
        reopened = Scheduler(control_store)
        self.assertEqual(
            reopened.state["calls"][prepared["call_id"]]["input"][
                "guidance_variant"
            ],
            "check-result-uncommon",
        )

        old_scheduler = Scheduler(FakeControlStore())
        old_scheduler.bootstrap(root_problem="Prove ROOT")
        old_scheduler.commit_initial_trim({"category_ids": []})
        old_scheduler.configure_alternation(
            {
                "attempts_per_worker": 3,
                "attempt_seconds": 10_800,
                "explorer_admission_seconds": 43_200,
                "franta_admission_seconds": 54_000,
            },
            now=at,
        )
        old_lineage = old_scheduler.admit_explorer_lineage(now=at)
        legacy = old_scheduler.start_explorer_attempt(
            old_lineage, source_high_water_seq=0, now=at
        )
        old_input = old_scheduler.state["calls"][legacy["call_id"]]["input"]
        for key in metadata:
            self.assertNotIn(key, old_input)
        self.assertNotIn("guidance_variant", old_input)


if __name__ == "__main__":
    unittest.main()
