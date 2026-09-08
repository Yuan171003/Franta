from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from franta.cli import main
from franta.config import load_manifest
from franta.dashboard_read import _read_connection
from franta.human_guidance import submit_human_guidance
from franta.runtime import FrantaRuntime
from franta.scheduler import Scheduler
from franta.store import MemoryStore
from explorer_system.repository import ExplorerRepository


def _initialize(root: Path, mode: str) -> FrantaRuntime:
    manifest = root / "bootstrap.toml"
    blocks = "\n[explorer]\nmax_workers = 1\n" if mode != "legacy" else ""
    if mode == "advisor":
        blocks += "\n[advisor]\n"
    manifest.write_text(
        "[project]\n"
        'name = "status-readonly"\n'
        'directory = "project"\n'
        'root_problem = "Prove that 1 = 1."\n'
        'foundation_policy = "Use ordinary equality axioms."\n'
        f"{blocks}\n[initial]\n",
        encoding="utf-8",
    )
    return FrantaRuntime.initialize(load_manifest(manifest))


def _control_row(database: Path) -> tuple:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT revision,payload_json,updated_at FROM control_state "
            "WHERE state_key='scheduler.v1'"
        ).fetchone()


def _project_files(project: Path) -> dict[str, tuple]:
    """Detect content changes and same-content rewrites, allowing WAL coordination."""

    result = {}
    for path in project.rglob("*"):
        if path.name.endswith(("-wal", "-shm")):
            continue
        relative = path.relative_to(project).as_posix()
        if path.is_file():
            result[relative] = (
                "file",
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        elif path.is_dir():
            result[relative] = ("directory",)
    return result


class CliStatusReadonlyTests(unittest.TestCase):
    def _status(self, project: Path) -> dict:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = main(["status", str(project)], output=output)
        self.assertEqual(result, 0, errors.getvalue())
        return json.loads(output.getvalue())

    def test_repeated_status_preserves_state_and_files_for_all_project_modes(self) -> None:
        for mode in ("legacy", "explorer", "advisor"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                with _initialize(Path(directory), mode) as runtime:
                    project = runtime.layout.root
                    runtime.scheduler.prepare_call("main", {}, call_id="CALL-BEFORE-STATUS")
                    submit_human_guidance(project, "Check the boundary case.")
                    expected = runtime.status()
                    before_row = _control_row(runtime.layout.database)
                    before_files = _project_files(project)

                    for _ in range(3):
                        self.assertEqual(self._status(project), expected)

                    self.assertEqual(_control_row(runtime.layout.database), before_row)
                    self.assertEqual(_project_files(project), before_files)

    def test_status_does_not_construct_runtime_or_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _initialize(Path(directory), "advisor") as runtime:
                expected = runtime.status()
                with ExitStack() as stack:
                    for writer in (FrantaRuntime, Scheduler, MemoryStore, ExplorerRepository):
                        stack.enter_context(
                            patch.object(
                                writer,
                                "__init__",
                                side_effect=AssertionError(f"constructed {writer.__name__}"),
                            )
                        )
                    self.assertEqual(self._status(runtime.layout.root), expected)

    def test_status_does_not_conflict_with_locked_active_scheduler(self) -> None:
        for mode in ("explorer", "advisor"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                with _initialize(Path(directory), mode) as runtime, runtime.lock:
                    before_revision = _control_row(runtime.layout.database)[0]
                    self._status(runtime.layout.root)
                    call_id = runtime.scheduler.prepare_call(
                        "main", {}, call_id="CALL-AFTER-STATUS"
                    )
                    self.assertEqual(call_id, "CALL-AFTER-STATUS")
                    self.assertEqual(
                        _control_row(runtime.layout.database)[0], before_revision + 1
                    )

    def test_status_reads_control_and_memory_counts_from_one_wal_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _initialize(Path(directory), "legacy") as runtime:
                runtime.scheduler.prepare_call("trimmer", {}, call_id="CALL-BEFORE-READ")
                before = runtime.status()
                after = None

                def commit_concurrent_update() -> None:
                    nonlocal after
                    result = runtime.store.add_memo(
                        "memo-during-status",
                        {
                            "abstract": "A concurrent note",
                            "genre": "high-level",
                            "content": "Check the equality boundary case.",
                            "related_route_ids": [],
                        },
                    )
                    self.assertEqual(result.status, "committed")
                    runtime.scheduler.prepare_call(
                        "main", {}, call_id="CALL-DURING-READ"
                    )
                    after = runtime.status()

                class InterleavedConnection:
                    def __init__(self, connection):
                        self.connection = connection
                        self.updated = False

                    def execute(self, sql, parameters=()):
                        # The reader has fetched control state, but has not yet
                        # read memory counts. Commit through the live WAL writer.
                        if "FROM memories" in sql and not self.updated:
                            self.updated = True
                            commit_concurrent_update()
                        return self.connection.execute(sql, parameters)

                @contextmanager
                def interleaved_read(path):
                    with _read_connection(path) as connection:
                        yield InterleavedConnection(connection)

                with patch(
                    "franta.dashboard_read._read_connection", interleaved_read
                ):
                    observed = self._status(runtime.layout.root)

                self.assertEqual(observed, before)
                self.assertIsNotNone(after)
                self.assertGreater(after["event_cursor"], before["event_cursor"])
                self.assertEqual(after["memories"], {**before["memories"], "memo": 1})
                self.assertEqual(self._status(runtime.layout.root), after)

    def test_status_does_not_repair_schema_or_explorer_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with _initialize(Path(directory), "advisor") as runtime:
                layout = runtime.layout
                expected = runtime.status()
            (layout.schemas / "explorer-worker.schema.json").unlink()
            layout.explorer_database.unlink()
            before = _project_files(layout.root)

            self.assertEqual(self._status(layout.root), expected)

            self.assertEqual(_project_files(layout.root), before)

    def test_missing_required_project_data_fails_without_creation_or_repair(self) -> None:
        for missing in ("project", "config", "database", "control_state"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                project = Path(directory) / "project"
                if missing != "project":
                    with _initialize(Path(directory), "advisor") as runtime:
                        layout = runtime.layout
                    if missing == "config":
                        (layout.private / "runtime-config.json").unlink()
                    elif missing == "database":
                        layout.database.unlink()
                    else:
                        with closing(sqlite3.connect(layout.database)) as connection:
                            connection.execute(
                                "DELETE FROM control_state WHERE state_key='scheduler.v1'"
                            )
                            connection.commit()
                before = _project_files(project)
                existed = project.exists()
                output = io.StringIO()
                errors = io.StringIO()

                with redirect_stderr(errors):
                    result = main(["status", str(project)], output=output)

                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("franta:", errors.getvalue())
                self.assertEqual(project.exists(), existed)
                self.assertEqual(_project_files(project), before)


if __name__ == "__main__":
    unittest.main()
