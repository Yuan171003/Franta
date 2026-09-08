from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from explorer_system.access import validate_check_result_request  # noqa: E402
from explorer_system.contracts import (  # noqa: E402
    ExplorerAccessError,
    ExplorerIdempotencyConflict,
    ExplorerPublishedMemoryDocument,
    ExplorerPublishedMemorySnapshot,
)
from explorer_system.repository import ExplorerRepository  # noqa: E402
from explorer_system.service import ExplorerAttempt, ExplorerService  # noqa: E402


def _receipt(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, event: dict[str, object]) -> None:
        self.events.append(event)


class ExplorerAccessPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ExplorerRepository(
            Path(self.temporary.name) / "explorer.sqlite3"
        )
        self.service = ExplorerService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    @staticmethod
    def _snapshot(*documents: ExplorerPublishedMemoryDocument, revision: str = "rev-1"):
        return ExplorerPublishedMemorySnapshot(
            revision=revision,
            documents=tuple(documents),
        )

    def _add_completed_attempt(
        self,
        *,
        turn: str,
        session: str,
        attempt_no: int,
        marker: str,
        direction: str,
        progress: str,
        obstacle: str = "An obstacle that must not enter the portfolio query.",
    ) -> tuple[str, str]:
        attempt = ExplorerAttempt(
            turn_id=turn,
            worker_session_id=session,
            attempt_no=attempt_no,
            call_id=f"call-{marker}",
        )
        scratch_id = f"ES-{marker}"
        scratch_receipt = _receipt(f"scratch-{marker}")
        self.service.prepare_staged_result(
            attempt,
            skill="record-scratch",
            receipt_sha256=scratch_receipt,
            artifact={
                "skill": "record-scratch",
                "operation_id": f"scratch-op-{marker}",
                "record_id": scratch_id,
                "record_kind": "progress",
                "abstract": f"Progress {marker}",
                "content": direction,
                "related_memory_ids": [],
                "cas_operation_ids": [],
            },
        )
        self.service.trust_receipt(scratch_receipt)
        summary_id = f"ESUM-{marker}"
        summary_receipt = _receipt(f"summary-{marker}")
        self.service.prepare_staged_result(
            attempt,
            skill="record-summary",
            receipt_sha256=summary_receipt,
            artifact={
                "skill": "record-summary",
                "operation_id": f"summary-op-{marker}",
                "record_id": summary_id,
                "abstract": f"Summary {marker}",
                "content": f"Full narrative for {marker}, including {obstacle}",
                "directions_tried": [direction],
                "main_progress": progress,
                "main_obstacles": obstacle,
                "source_scratch_ids": [scratch_id],
            },
        )
        self.service.trust_receipt(summary_receipt)
        return scratch_id, summary_id

    def test_check_result_is_unlimited_strict_opaque_and_epistemically_uniform(self) -> None:
        snapshot = self._snapshot(
            ExplorerPublishedMemoryDocument(
                "F-secret",
                "fact",
                "Compact Hausdorff spaces are normal",
                "Every compact Hausdorff space is a normal topological space.",
            ),
            ExplorerPublishedMemoryDocument(
                "CL-secret",
                "claim",
                "Normality of compact Hausdorff spaces",
                "A compact Hausdorff space is normal by the shrinking lemma.",
            ),
            ExplorerPublishedMemoryDocument(
                "C-secret",
                "computation",
                "Euler characteristic equals two",
                "For the specified complex, the Euler characteristic is 2.",
            ),
            ExplorerPublishedMemoryDocument(
                "C-normality",
                "computation",
                "Normality check for compact Hausdorff spaces",
                "The computation confirms that every compact Hausdorff space is normal.",
            ),
            ExplorerPublishedMemoryDocument(
                "CL-withdrawn",
                "claim",
                "Compact Hausdorff spaces are metrizable",
                "Every compact Hausdorff space is metrizable.",
                eligible=False,
            ),
        )
        attempt = ExplorerAttempt("turn-1", "worker-1", 1, "call-1")
        grant = self.service.create_attempt_access_grant(
            attempt, host_snapshot=snapshot
        )
        self.assertEqual(grant.access_mode, "check-result")
        self.assertNotIn("CL-withdrawn", {item.source_id for item in grant.published_documents})

        audit = _Audit()
        api = self.service.attempt_access_api(attempt, audit=audit)
        for _ in range(25):  # No cumulative attempt quota exists.
            matches = api.check_result(
                "proved", "Every compact Hausdorff space is normal."
            )
            self.assertEqual(len(matches), 3)
        for match in matches:
            self.assertEqual(
                set(match),
                {"result_id", "status", "abstract", "main_content", "relevance"},
            )
            self.assertEqual(match["status"], "established")
            self.assertTrue(str(match["result_id"]).startswith("XCR-"))
            rendered = json.dumps(match, sort_keys=True)
            self.assertNotIn("F-secret", rendered)
            self.assertNotIn("CL-secret", rendered)
            self.assertNotIn("record_type", rendered)
            self.assertNotIn("memory_kind", rendered)
        self.assertEqual(
            {item["memory_kind"] for item in audit.events[-1]["private_matches"]},
            {"fact", "claim", "computation"},
        )

        computed = api.check_result(
            "computed", "The Euler characteristic of the specified complex equals 2."
        )
        self.assertEqual(len(computed), 1)
        self.assertEqual(computed[0]["status"], "established")
        self.assertEqual(set(computed[0]), set(matches[0]))
        self.assertEqual(
            api.check_result("proved", "Every quaternionic moon is triangular."),
            [],
        )
        with self.assertRaises(ExplorerAccessError):
            api.portfolio_search("compact")

    def test_check_result_requires_one_strict_mathematical_proposition(self) -> None:
        self.assertEqual(
            validate_check_result_request(
                "proved", "Every compact Hausdorff space is normal."
            ),
            ("proved", "Every compact Hausdorff space is normal."),
        )
        for bad in (
            "Whether compact Hausdorff spaces are normal",
            "What is known about compact spaces?",
            "compact Hausdorff normality",
            "Every compact space is normal; every normal space is compact.",
            "- Every compact Hausdorff space is normal.",
        ):
            with self.subTest(statement=bad), self.assertRaises(ExplorerAccessError):
                validate_check_result_request("proved", bad)
        with self.assertRaises(ExplorerAccessError):
            validate_check_result_request(
                "unknown", "Every compact Hausdorff space is normal."
            )

    def test_grants_are_append_only_digest_pinned_and_idempotent(self) -> None:
        attempt = ExplorerAttempt("turn-grant", "worker-grant", 1, "call-grant")
        snapshot = self._snapshot(
            ExplorerPublishedMemoryDocument(
                "F-1", "fact", "Prime parity", "Every prime greater than 2 is odd."
            )
        )
        first = self.service.create_attempt_access_grant(
            attempt, host_snapshot=snapshot
        )
        replay = self.service.create_attempt_access_grant(
            attempt, host_snapshot=snapshot
        )
        self.assertEqual(first, replay)
        self.assertEqual(
            self.repository.get_attempt_access_grant_by_id(first.grant_id), first
        )

        changed = self._snapshot(
            ExplorerPublishedMemoryDocument(
                "F-2", "fact", "Prime parity", "Every prime greater than 2 is odd."
            ),
            revision="rev-2",
        )
        with self.assertRaises(ExplorerIdempotencyConflict):
            self.service.create_attempt_access_grant(
                attempt, host_snapshot=changed
            )

    def test_attempt_two_portfolio_is_deterministic_cross_lineage_and_deidentified(self) -> None:
        turn = "turn-portfolio"
        self._add_completed_attempt(
            turn=turn,
            session="worker-own",
            attempt_no=1,
            marker="own-a1",
            direction="Use a spectral sequence filtration.",
            progress="The spectral boundary map controls degeneration.",
            obstacle="OBSTACLE-SHOULD-NOT-ENTER-QUERY",
        )
        self._add_completed_attempt(
            turn=turn,
            session="worker-peer-close",
            attempt_no=1,
            marker="peer-close",
            direction="Analyze the same spectral filtration from the dual complex.",
            progress="The spectral boundary is dual to an obstruction class.",
        )
        self._add_completed_attempt(
            turn=turn,
            session="worker-peer-random",
            attempt_no=2,
            marker="peer-random",
            direction="Construct a combinatorial polytope model.",
            progress="Small polytope examples reveal a parity pattern.",
        )

        documents: list[ExplorerPublishedMemoryDocument] = []
        for index in range(6):
            documents.append(
                ExplorerPublishedMemoryDocument(
                    f"R-private-{index}",
                    "route",
                    f"Route abstract {index}",
                    (
                        "Develop the spectral filtration and boundary map."
                        if index == 0
                        else f"Unrelated route mechanism {index}."
                    ),
                )
            )
        for index in range(10):
            documents.append(
                ExplorerPublishedMemoryDocument(
                    f"M-private-{index}",
                    "memo",
                    f"Memo abstract {index}",
                    (
                        f"Spectral filtration boundary degeneration note {index}."
                        if index < 3
                        else f"Distant memo mechanism {index}."
                    ),
                )
            )
        documents.extend(
            [
                ExplorerPublishedMemoryDocument(
                    "F-check", "fact", "Boundary vanishing", "The boundary map is zero."
                ),
                ExplorerPublishedMemoryDocument(
                    "CL-check", "claim", "Boundary vanishing claim", "The boundary map vanishes."
                ),
            ]
        )
        snapshot = self._snapshot(*documents, revision="rev-portfolio")
        attempt = ExplorerAttempt(turn, "worker-own", 2, "call-own-a2")
        high_water = self.repository.visible_high_water()
        grant = self.service.create_attempt_access_grant(
            attempt,
            host_snapshot=snapshot,
            source_high_water_seq=high_water,
            experiment_seed="experiment-17",
        )
        assert grant.portfolio is not None
        portfolio = grant.portfolio
        self.assertEqual(grant.access_mode, "portfolio")
        self.assertEqual(portfolio.explorer_high_water_seq, high_water)
        self.assertNotIn("OBSTACLE-SHOULD-NOT-ENTER-QUERY", portfolio.query_text)
        self.assertEqual(
            portfolio.query_text,
            "Use a spectral sequence filtration.\n"
            "The spectral boundary map controls degeneration.",
        )
        kinds = [item.item_kind for item in portfolio.items]
        self.assertEqual(kinds.count("route"), 3)
        self.assertEqual(kinds.count("memo"), 6)
        self.assertEqual(kinds.count("peer-summary"), 2)
        self.assertEqual(kinds.count("peer-scratch"), 2)
        peer_summaries = [
            item for item in portfolio.items if item.item_kind == "peer-summary"
        ]
        self.assertEqual(
            {item.source_worker_session_id for item in peer_summaries},
            {"worker-peer-close", "worker-peer-random"},
        )
        self.assertEqual(
            next(item for item in peer_summaries if item.selection_reason == "closest")
            .source_worker_session_id,
            "worker-peer-close",
        )

        api = self.service.attempt_access_api(attempt)
        visible = api.portfolio_search("spectral filtration", limit=10)
        self.assertTrue(visible)
        for item in visible:
            self.assertEqual(
                set(item),
                {"portfolio_item_id", "abstract", "main_content", "relevance"},
            )
            rendered = json.dumps(item, sort_keys=True)
            self.assertNotIn("R-private-", rendered)
            self.assertNotIn("M-private-", rendered)
            self.assertNotIn("selection_reason", rendered)
            self.assertNotIn("source_id", rendered)
            self.assertNotIn("relation", rendered)
        fetched = api.portfolio_fetch(visible[0]["portfolio_item_id"])
        self.assertEqual(
            set(fetched), {"portfolio_item_id", "abstract", "main_content"}
        )
        with self.assertRaises(ExplorerAccessError):
            api.portfolio_fetch("R-private-0")

        # A later trusted peer record cannot leak into the frozen grant.
        self._add_completed_attempt(
            turn=turn,
            session="worker-peer-late",
            attempt_no=1,
            marker="peer-late",
            direction="Use a late spectral sequence.",
            progress="This was committed after the portfolio high-water.",
        )
        restored = self.repository.get_attempt_access_grant(
            turn, "worker-own", 2
        )
        self.assertEqual(restored, grant)
        assert restored is not None and restored.portfolio is not None
        self.assertNotIn(
            "worker-peer-late",
            {
                item.source_worker_session_id
                for item in restored.portfolio.items
            },
        )

    def test_attempt_three_grant_is_full_memory_and_not_a_portfolio(self) -> None:
        snapshot = self._snapshot(
            ExplorerPublishedMemoryDocument(
                "R-1", "route", "Route", "Use the universal family."
            )
        )
        attempt = ExplorerAttempt("turn-3", "worker-3", 3, "call-3")
        grant = self.service.create_attempt_access_grant(
            attempt, host_snapshot=snapshot
        )
        self.assertEqual(grant.access_mode, "full-memory")
        self.assertIsNone(grant.portfolio)
        api = self.service.attempt_access_api(attempt)
        with self.assertRaises(ExplorerAccessError):
            api.check_result("proved", "The universal family is smooth.")
        with self.assertRaises(ExplorerAccessError):
            api.portfolio_search("universal")

    def test_attempt_two_query_degrades_to_own_scratch_then_exact_host_fallback(self) -> None:
        snapshot = self._snapshot(
            ExplorerPublishedMemoryDocument(
                "R-fallback", "route", "Route", "Use the filtered complex."
            )
        )
        first = ExplorerAttempt(
            "turn-scratch-fallback", "worker-scratch-fallback", 1, "call-a1"
        )
        receipt = _receipt("scratch-fallback-direction")
        self.service.prepare_staged_result(
            first,
            skill="record-scratch",
            receipt_sha256=receipt,
            artifact={
                "skill": "record-scratch",
                "operation_id": "scratch-fallback-op",
                "record_id": "ES-scratch-fallback",
                "record_kind": "progress",
                "abstract": "Filtered direction",
                "content": "Study the filtered complex boundary map.",
                "related_memory_ids": [],
                "cas_operation_ids": [],
            },
        )
        self.service.trust_receipt(receipt)
        second = ExplorerAttempt(
            first.turn_id, first.worker_session_id, 2, "call-a2"
        )
        scratch_grant = self.service.create_attempt_access_grant(
            second,
            host_snapshot=snapshot,
            fallback_query="This host fallback must not be selected.",
        )
        assert scratch_grant.portfolio is not None
        self.assertEqual(
            scratch_grant.portfolio.query_source,
            "attempt-1-scratch-fallback",
        )
        self.assertEqual(
            scratch_grant.portfolio.query_text,
            "Filtered direction\nStudy the filtered complex boundary map.",
        )
        self.assertEqual(
            scratch_grant.portfolio.query_source_record_ids,
            ("ES-scratch-fallback",),
        )
        self.assertIsNone(scratch_grant.portfolio.source_summary_id)

        empty_second = ExplorerAttempt(
            "turn-host-fallback", "worker-host-fallback", 2, "call-empty-a2"
        )
        root = "Every smooth proper curve has a finite arithmetic genus."
        host_grant = self.service.create_attempt_access_grant(
            empty_second,
            host_snapshot=snapshot,
            fallback_query=root,
        )
        assert host_grant.portfolio is not None
        self.assertEqual(host_grant.portfolio.query_source, "host-fallback")
        self.assertEqual(host_grant.portfolio.query_text, root)
        self.assertEqual(host_grant.portfolio.query_source_record_ids, ())
        self.assertIsNone(host_grant.portfolio.source_summary_digest)
        with self.assertRaises(ExplorerIdempotencyConflict):
            self.service.create_attempt_access_grant(
                empty_second,
                host_snapshot=snapshot,
                fallback_query="Every smooth proper surface has finite geometric genus.",
            )


if __name__ == "__main__":
    unittest.main()
