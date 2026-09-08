"""Bounded CAS subprocesses owned by one agent call."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence


CAS_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 0.05
_TERMINATION_GRACE_SECONDS = 1.0


class CASCancelled(subprocess.SubprocessError):
    """The owning agent call has ended."""


class CASProcessScope:
    """Prevent new launches after cancellation and wait for owned processes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._active: set[threading.Event] = set()

    def check(self) -> None:
        if self._closed.is_set():
            raise CASCancelled("CAS cancelled because its agent call ended")

    def close(self) -> None:
        # Launch and registration share this lock, so cancellation cannot miss
        # a process between checking the scope and recording its ownership.
        with self._lock:
            self._closed.set()
            active = tuple(self._active)
        for finished in active:
            if not finished.wait(4 * _TERMINATION_GRACE_SECONDS + 2):
                raise CASCancelled("timed out waiting for CAS process cleanup")

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> bool:
        deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
        while True:
            process.poll()
            try:
                os.killpg(process.pid, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                # On macOS a group whose last member is exiting can briefly
                # return EPERM before waitpid can reap it. Retry that window;
                # a persistent permission failure must still be reported.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_POLL_SECONDS)

    @staticmethod
    def _cleanup(process: subprocess.Popen[str]) -> None:
        if os.name != "posix":
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            return

        # start_new_session makes the PID the private PGID.  Keep using that
        # ID after the leader exits: surviving descendants still belong to it.
        pgid = process.pid
        if not CASProcessScope._signal_group(process, signal.SIGTERM):
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            return
        deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                # Keep polling/reaping during the macOS exit transition.
                # Do not mistake EPERM for an empty process group.
                pass
            time.sleep(_POLL_SECONDS)
        else:
            CASProcessScope._signal_group(process, signal.SIGKILL)
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None,
        cwd: Path,
        env: Mapping[str, str],
        deadline: float,
    ) -> subprocess.CompletedProcess[str]:
        finished = threading.Event()
        with self._lock:
            self.check()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, CAS_TIMEOUT_SECONDS)
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=dict(env),
                shell=False,
                start_new_session=os.name == "posix",
            )
            self._active.add(finished)
        try:
            pending_input = input_text
            while True:
                self.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, CAS_TIMEOUT_SECONDS)
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input,
                        timeout=min(_POLL_SECONDS, remaining),
                    )
                except subprocess.TimeoutExpired:
                    # communicate retains partially written input and captured
                    # output across retries; input must only be supplied once.
                    pending_input = None
                    continue
                self.check()
                return subprocess.CompletedProcess(
                    list(command), process.returncode, stdout, stderr
                )
        finally:
            try:
                self._cleanup(process)
            finally:
                try:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                finally:
                    with self._lock:
                        self._active.discard(finished)
                        finished.set()
