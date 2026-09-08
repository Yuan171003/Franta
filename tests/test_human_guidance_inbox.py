from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from franta.cli import main
from franta.config import load_manifest
from franta.human_guidance import read_human_guidance_inbox, submit_human_guidance
from franta.runtime import FrantaRuntime


class HumanGuidanceInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        manifest = self.root / "bootstrap.toml"
        manifest.write_text(
            '[project]\nname = "guidance-inbox-test"\ndirectory = "project"\n'
            'root_problem = "Prove P."\nfoundation_policy = "Use the definition of P."\n'
            '[context_budgets]\nmain = 1000\n[initial]\n',
            encoding="utf-8",
        )
        self.runtime = FrantaRuntime.initialize(load_manifest(manifest))
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.layout.root
        self.inbox = self.project / "private/human-guidance/inbox"

    def test_exact_unicode_multiline_and_filename_timestamp_survive_mtime_change(self) -> None:
        text = "  # 人类建议 🧮\r\n\r\n先研究 $X \\to S$。\n再考虑一般情形。\n\n"
        snapshot = submit_human_guidance(self.project, text)
        self.assertEqual(
            set(snapshot),
            {"guidance_id", "text", "sha256", "relative_path", "received_at"},
        )
        path = self.project / snapshot["relative_path"]
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        self.assertEqual(snapshot["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertTrue(snapshot["received_at"].endswith("Z"))
        os.utime(path, (1, 1))
        self.assertEqual(read_human_guidance_inbox(self.project), [snapshot])

    def test_concurrent_submissions_preserve_every_distinct_record(self) -> None:
        count = 12
        barrier = threading.Barrier(count)

        def submit(index: int) -> dict:
            barrier.wait(timeout=10)
            return submit_human_guidance(self.project, f"建议 {index}\n\n路线 {index}\n")

        with self.runtime.lock, ThreadPoolExecutor(max_workers=count) as pool:
            records = list(pool.map(submit, range(count)))
        self.assertEqual(len({record["guidance_id"] for record in records}), count)
        self.assertEqual(
            read_human_guidance_inbox(self.project),
            sorted(records, key=lambda record: (record["received_at"], record["guidance_id"])),
        )
        self.assertEqual(len(list(self.inbox.iterdir())), count)

    def test_cli_during_open_locked_runtime_never_opens_runtime_or_changes_revision(self) -> None:
        text = "# 精确内容\r\n\r\n保留尾部空白  \r\n"
        source = self.root / "suggestion.md"
        source.write_bytes(text.encode("utf-8"))
        before = self.runtime.store.load_control_state(self.runtime.scheduler.CONTROL_KEY)
        revision = self.runtime.scheduler.revision
        output = io.StringIO()
        with self.runtime.lock, mock.patch(
            "franta.cli.FrantaRuntime.open", side_effect=AssertionError("must not open runtime")
        ):
            self.assertEqual(main(["suggest", str(self.project), f"@{source}"], output=output), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["text"], text)
        self.assertEqual(Path(result["path"]).read_bytes(), source.read_bytes())
        self.assertEqual(self.runtime.scheduler.revision, revision)
        self.assertEqual(
            self.runtime.store.load_control_state(self.runtime.scheduler.CONTROL_KEY), before
        )

    def test_empty_invalid_text_and_uninitialized_project_do_not_create_an_inbox(self) -> None:
        for text in ("", " \r\n\t ", "bad\x00text", None, b"bytes"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                submit_human_guidance(self.project, text)
        with self.assertRaises(UnicodeEncodeError):
            submit_human_guidance(self.project, "bad\ud800text")
        self.assertFalse(self.inbox.parent.exists())
        uninitialized = self.root / "not-initialized"
        uninitialized.mkdir()
        with self.assertRaises((OSError, ValueError)):
            submit_human_guidance(uninitialized, "A suggestion")
        self.assertEqual(list(uninitialized.iterdir()), [])

    def test_reader_of_missing_inbox_never_creates_directories(self) -> None:
        self.assertEqual(read_human_guidance_inbox(self.project), [])
        self.assertFalse(self.inbox.parent.exists())

    def test_polling_preserves_legacy_projects_without_source_copies(self) -> None:
        before = self.runtime.status()
        for marker in ("root-problem.md", "foundation-v1.md", "bootstrap-manifest.toml"):
            (self.project / marker).unlink()
        revision = self.runtime.scheduler.revision
        self.assertEqual(self.runtime.status(), before)
        self.assertFalse(self.runtime._ingest_human_guidance())
        self.assertEqual(self.runtime.scheduler.revision, revision)
        self.assertFalse(self.inbox.parent.exists())

    def test_reader_needs_only_real_inbox_directories_and_files(self) -> None:
        snapshot = submit_human_guidance(self.project, "Preserve this received suggestion.")
        bare = self.root / "read-only-copy"
        bare.mkdir()
        self.assertEqual(read_human_guidance_inbox(bare), [])
        copied_inbox = bare / "private/human-guidance/inbox"
        copied_inbox.mkdir(parents=True)
        (copied_inbox / f"{snapshot['guidance_id']}.md").write_bytes(
            snapshot["text"].encode("utf-8")
        )
        self.assertEqual(read_human_guidance_inbox(bare), [snapshot])
        with self.assertRaises(OSError):
            submit_human_guidance(bare, "Submission still requires initialization.")

    def test_reader_ignores_partial_file_until_atomic_publication(self) -> None:
        ready = threading.Event()
        publish = threading.Event()
        real_link = os.link

        def delayed_link(*args: object, **kwargs: object) -> None:
            ready.set()
            if not publish.wait(timeout=10):
                raise AssertionError("publication was not released")
            real_link(*args, **kwargs)

        with mock.patch("franta.human_guidance.os.link", side_effect=delayed_link):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(submit_human_guidance, self.project, "Complete suggestion\n")
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    self.assertEqual(read_human_guidance_inbox(self.project), [])
                    partials = list(self.inbox.iterdir())
                    self.assertEqual(len(partials), 1)
                    self.assertTrue(partials[0].name.startswith("."))
                finally:
                    publish.set()
                snapshot = future.result(timeout=10)
        (self.inbox / ".interrupted-upload.tmp").write_bytes(b"\xffpartial")
        self.assertEqual(read_human_guidance_inbox(self.project), [snapshot])

    def test_symlink_directories_and_completed_files_cannot_escape_project(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        guidance_dir = self.project / "private/human-guidance"
        guidance_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            submit_human_guidance(self.project, "Do not write outside")
        self.assertEqual(read_human_guidance_inbox(self.project), [])
        self.assertEqual(list(outside.iterdir()), [])
        (outside / "inbox").mkdir()
        with self.assertRaises(OSError):
            read_human_guidance_inbox(self.project)
        (outside / "inbox").rmdir()
        guidance_dir.unlink()

        guidance_dir.mkdir()
        self.inbox.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            submit_human_guidance(self.project, "Still do not write outside")
        with self.assertRaises(OSError):
            read_human_guidance_inbox(self.project)
        self.assertEqual(list(outside.iterdir()), [])
        self.inbox.unlink()

        snapshot = submit_human_guidance(self.project, "Safe suggestion")
        completed = self.project / snapshot["relative_path"]
        completed.unlink()
        external_file = outside / "external.md"
        external_file.write_text("Untrusted file", encoding="utf-8")
        completed.symlink_to(external_file)
        with self.assertRaises(OSError):
            read_human_guidance_inbox(self.project)

    def test_completed_invalid_utf8_is_rejected(self) -> None:
        snapshot = submit_human_guidance(self.project, "Original valid text")
        completed = self.project / snapshot["relative_path"]
        completed.chmod(0o600)
        completed.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            read_human_guidance_inbox(self.project)


if __name__ == "__main__":
    unittest.main()
