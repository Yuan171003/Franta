"""Broker transport remains usable in long and non-ASCII project paths."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from franta.access import BrokerClient, BrokerError, InMemoryBackend, MemoryBroker, policy_for
from franta.execution_gateway import broker as broker_module


class BrokerPortabilityTests(unittest.TestCase):
    def _exercise_long_path(self, socket_path: Path) -> None:
        self.assertGreater(len(os.fsencode(socket_path.resolve())), 103)
        broker = MemoryBroker(InMemoryBackend())
        self.addCleanup(broker.stop)
        with mock.patch.object(broker_module, "_BrokerUnixServer") as server:
            broker.start(socket_path)
            server.assert_not_called()
        expected_spool = Path(str(socket_path.resolve()) + ".spool")
        self.assertEqual(broker.socket_path, "file://" + str(expected_spool))
        self.assertEqual(stat.S_IMODE(expected_spool.stat().st_mode), 0o700)
        binding = broker.issue(policy_for("verifier"), caller_id="portable-test")
        client = BrokerClient(binding.socket_path, binding.token, timeout=2)
        description = client.call("describe", {})
        self.assertEqual(description["enabled_tools"], sorted(binding.enabled_tools))
        with self.assertRaisesRegex(BrokerError, "invalid or revoked"):
            BrokerClient(binding.socket_path, "invalid-token", timeout=2).call("describe", {})
        with self.assertRaisesRegex(RuntimeError, "already started"):
            broker.start(socket_path.with_name("second.sock"))
        self.assertEqual(client.call("describe", {}), description)
        self.assertFalse(list(expected_spool.iterdir()))
        broker.stop()
        self.assertFalse(expected_spool.exists())
        self.assertIsNone(broker.socket_path)
        self.assertIsNone(broker._thread)
        # The same project path must be reusable after a clean scheduler stop.
        broker.start(socket_path)
        second = broker.issue(policy_for("verifier"), caller_id="restarted-test")
        self.assertEqual(BrokerClient(second.socket_path, second.token, timeout=2).call("describe", {}), description)
        broker.stop()
        self.assertFalse(expected_spool.exists())

    def test_real_long_ascii_project_path_uses_authenticated_spool_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="franta-long-broker-project-") as raw:
            socket_path = Path(raw) / ("a" * 80) / "example-project/private/memory-broker.sock"
            self._exercise_long_path(socket_path)

    def test_multibyte_path_limit_counts_bytes_instead_of_characters(self) -> None:
        # A short temporary parent leaves room for a path with <104 characters
        # but >103 encoded bytes on both macOS and Linux.
        with tempfile.TemporaryDirectory(prefix="franta-broker-", dir="/tmp") as raw:
            socket_path = Path(raw) / ("数" * 30) / "broker.sock"
            self.assertLessEqual(len(str(socket_path.resolve())), 103)
            self._exercise_long_path(socket_path)

    def test_bind_path_length_and_permission_errors_use_existing_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fb-", dir="/tmp") as raw:
            for index, error in enumerate((
                OSError(errno.ENAMETOOLONG, "name too long"),
                PermissionError("managed host forbids binding"),
                OSError(errno.EACCES, "access denied"),
                OSError(errno.EPERM, "operation not permitted"),
            )):
                with self.subTest(error=error):
                    broker = MemoryBroker(InMemoryBackend())
                    self.addCleanup(broker.stop)
                    socket_path = Path(raw) / f"broker-{index}.sock"
                    with mock.patch.object(broker_module, "_BrokerUnixServer", side_effect=error):
                        broker.start(socket_path)
                    self.assertTrue(broker.socket_path.startswith("file://"))
                    broker.stop()

    def test_other_bind_errors_are_not_hidden_by_spool_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fb-", dir="/tmp") as raw:
            for index, number in enumerate((errno.EADDRINUSE, errno.EMFILE, errno.EIO)):
                with self.subTest(errno=number):
                    broker = MemoryBroker(InMemoryBackend())
                    error = OSError(number, "unexpected bind failure")
                    socket_path = Path(raw) / f"broker-{index}.sock"
                    with mock.patch.object(broker_module, "_BrokerUnixServer", side_effect=error):
                        with self.assertRaises(OSError) as raised:
                            broker.start(socket_path)
                    self.assertIs(raised.exception, error)
                    self.assertIsNone(broker.socket_path)
                    self.assertFalse(Path(str(socket_path) + ".spool").exists())

    def test_existing_spool_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="franta-long-broker-project-") as raw:
            socket_path = Path(raw) / ("a" * 80) / "broker.sock"
            spool = Path(str(socket_path.resolve()) + ".spool")
            spool.mkdir(parents=True)
            sentinel = spool / "existing-state"
            sentinel.write_text("keep", encoding="utf-8")
            broker = MemoryBroker(InMemoryBackend())
            with self.assertRaisesRegex(RuntimeError, "spool path already exists"):
                broker.start(socket_path)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertIsNone(broker.socket_path)


if __name__ == "__main__":
    unittest.main()
