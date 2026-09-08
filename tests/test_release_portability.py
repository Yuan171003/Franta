"""Release regressions for executable discovery and platform confinement."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from franta.config import ConfigurationError, load_manifest
from franta.contracts.agent_access import policy_for
from franta.execution_gateway import skills


MINIMAL_MANIFEST = '''
[project]
name = "portable-project"
directory = "state"
root_problem = "Prove that 1 + 1 = 2."
foundation_policy = "Ordinary arithmetic."
'''


class ReleaseToolConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manifest = self.root / "bootstrap.toml"

    def _load(self, configuration: str):
        self.manifest.write_text(MINIMAL_MANIFEST + configuration, encoding="utf-8")
        return load_manifest(self.manifest)

    def test_path_names_remain_portable_and_optional_tools_stay_disabled(self) -> None:
        defaults = self._load("").tools
        self.assertEqual(defaults.codex, "codex")
        self.assertIsNone(defaults.sage)
        self.assertIsNone(defaults.macaulay2)
        configured = self._load('''
[tools]
codex = "codex"
sage = "sage"
macaulay2 = "M2"
tectonic = "tectonic"
[tools.extra_cas]
singular = "Singular"
''').tools
        self.assertEqual(configured.macaulay2, "M2")
        self.assertEqual(configured.sage, "sage")
        self.assertEqual(configured.tectonic, "tectonic")
        self.assertEqual(configured.extra_cas, {"singular": "Singular"})

    def test_explicit_tool_paths_are_relative_to_manifest_and_expand_home(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "operator-home")}):
            configured = self._load('''
[tools]
codex = "./tools/codex"
sage = "environments/sage/bin/sage"
macaulay2 = "~/bin/M2"
tectonic = "./tools with spaces/tectonic"
[tools.extra_cas]
singular = "./tools/Singular"
''').tools
        self.assertEqual(configured.codex, str(self.root / "tools/codex"))
        self.assertEqual(configured.sage, str(self.root / "environments/sage/bin/sage"))
        self.assertEqual(configured.macaulay2, str(self.root / "operator-home/bin/M2"))
        self.assertEqual(configured.tectonic, str(self.root / "tools with spaces/tectonic"))
        self.assertEqual(configured.extra_cas["singular"], str(self.root / "tools/Singular"))

    def test_invalid_tool_configuration_reports_configuration_error(self) -> None:
        for configuration in (
            '[tools]\nsage = ""',
            '[tools]\ncodex = false',
            '[tools]\nunknown = "binary"',
            '[tools.extra_cas]\nother = ["python", "-c"]',
            '[tools.extra_cas]\n"" = "binary"',
        ):
            with self.subTest(configuration=configuration):
                with self.assertRaises(ConfigurationError):
                    self._load(configuration)

    def test_cas_path_resolution_uses_path_and_handles_spaces_without_shell(self) -> None:
        directory = self.root / "tool binaries"
        directory.mkdir()
        executable = directory / "M2"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(directory)}):
            self.assertEqual(skills._trusted_executable("M2", "Macaulay2"), executable)
            self.assertEqual(skills._trusted_executable(executable, "Macaulay2"), executable)
            with self.assertRaisesRegex(skills.SkillRuntimeError, "unavailable"):
                skills._trusted_executable("M2 --version", "Macaulay2")
        executable.chmod(0o644)
        with self.assertRaisesRegex(skills.SkillRuntimeError, "unavailable"):
            skills._trusted_executable(str(executable), "Macaulay2")


class ReleaseConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "call with spaces"
        self.workspace.mkdir()
        for name in ("input", "outbox", "artifacts", "tmp"):
            (self.workspace / name).mkdir()
        self.context = skills.SkillContext(self.workspace, policy_for("verifier"))

    def _linux_command(self, **kwargs):
        with (
            mock.patch.object(skills.sys, "platform", "linux"),
            mock.patch.object(skills.shutil, "which", return_value="/usr/bin/bwrap"),
        ):
            return skills._constrained_command(
                Path(sys.executable).resolve(), ["-c", "print('a b')"],
                workspace=self.workspace, **kwargs,
            )

    def test_linux_has_private_namespaces_and_only_call_outputs_are_host_writable(self) -> None:
        command = self._linux_command()
        self.assertEqual(command[0], "/usr/bin/bwrap")
        for required in ("--unshare-all", "--die-with-parent", "--new-session"):
            self.assertIn(required, command)
        bindings = [command[index + 1:index + 3] for index, part in enumerate(command) if part == "--bind"]
        self.assertEqual(bindings, [[str(self.workspace / name)] * 2 for name in ("outbox", "artifacts", "tmp")])
        read_bindings = [command[index + 1] for index, part in enumerate(command) if part == "--ro-bind"]
        self.assertNotIn("/", read_bindings)
        self.assertNotIn(str(Path.home()), read_bindings)
        self.assertIn(str(self.workspace), read_bindings)
        self.assertEqual(command[-4:], ["--", str(Path(sys.executable).resolve()), "-c", "print('a b')"])

    def test_linux_masks_explicitly_denied_data_within_readable_trees(self) -> None:
        denied_directory = self.workspace / "input" / "private"
        denied_directory.mkdir()
        denied_file = self.workspace / "input" / "private.json"
        denied_file.write_text("secret", encoding="utf-8")
        command = self._linux_command(denied_paths=[denied_directory, denied_file])
        index = command.index("--tmpfs")
        self.assertEqual(command[index:index + 4], ["--tmpfs", str(denied_directory), "--remount-ro", str(denied_directory)])
        index = command.index("/dev/null")
        self.assertEqual(command[index - 1:index + 2], ["--ro-bind", "/dev/null", str(denied_file)])

    def test_linux_refuses_writable_symlink_outside_call(self) -> None:
        shutil.rmtree(self.workspace / "outbox")
        (self.workspace / "outbox").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(skills.SkillRuntimeError, "unsafe"):
            self._linux_command()

    def test_linux_without_bubblewrap_and_unsupported_platforms_fail_closed(self) -> None:
        for platform in ("linux", "win32"):
            with (
                self.subTest(platform=platform),
                mock.patch.object(skills.sys, "platform", platform),
                mock.patch.object(skills.shutil, "which", return_value=None),
            ):
                with self.assertRaisesRegex(skills.SkillRuntimeError, "confinement is unavailable"):
                    skills._constrained_command(Path(sys.executable), [], workspace=self.workspace)

    def test_failed_linux_sandbox_reports_actionable_error_without_retry(self) -> None:
        failure = subprocess.CompletedProcess(["bwrap"], 1, "", "bwrap: Creating new namespace failed: Operation not permitted")
        with (
            mock.patch.object(skills.sys, "platform", "linux"),
            mock.patch.object(skills.shutil, "which", return_value="/usr/bin/bwrap"),
            mock.patch.object(skills.subprocess, "run", return_value=failure) as runner,
        ):
            with self.assertRaisesRegex(skills.SkillRuntimeError, "user-namespace support"):
                skills._run_constrained(self.context, Path(sys.executable), ["--version"])
        self.assertEqual(runner.call_count, 1)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and os.environ.get("FRANTA_RUN_CONFINEMENT_INTEGRATION") == "1",
        "requires Linux with bubblewrap and working user namespaces",
    )
    def test_real_linux_confinement_blocks_private_data_network_and_input_writes(self) -> None:
        secret = self.root / "host-secret"
        secret.write_text("secret", encoding="utf-8")
        immutable = self.workspace / "input" / "immutable"
        immutable.write_text("keep", encoding="utf-8")
        code = (
            "import json, pathlib, socket\n"
            "results = {}\n"
            f"checks = {{'host_read': lambda: pathlib.Path({str(secret)!r}).read_text(), "
            f"'input_write': lambda: pathlib.Path({str(immutable)!r}).write_text('changed'), "
            "'network': lambda: socket.create_connection(('1.1.1.1', 443), timeout=1)}\n"
            "for key, check in checks.items():\n"
            "    try: check(); results[key] = 'allowed'\n"
            "    except OSError: results[key] = 'blocked'\n"
            "pathlib.Path('artifacts/result.json').write_text(json.dumps(results))\n"
        )
        result = skills._run_constrained(self.context, Path(sys.executable).resolve(), ["-c", code], timeout_seconds=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.workspace / "artifacts/result.json").read_text()), {
            "host_read": "blocked", "input_write": "blocked", "network": "blocked",
        })
        self.assertEqual(immutable.read_text(), "keep")


class ReleaseTectonicCacheTests(unittest.TestCase):
    def test_platform_caches_and_explicit_override_are_narrowly_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            mac_cache = root / "Library/Caches/Tectonic"
            linux_cache = root / ".cache/tectonic"
            xdg_cache = root / "xdg/Tectonic"
            override = root / "custom-cache"
            for path in (mac_cache, linux_cache, xdg_cache, override):
                path.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(root)}, clear=True):
                with mock.patch.object(skills.sys, "platform", "darwin"):
                    self.assertEqual(skills._tectonic_cache_paths(), [mac_cache])
                with mock.patch.object(skills.sys, "platform", "linux"):
                    caches = skills._tectonic_cache_paths()
                    self.assertEqual(len(caches), 1)
                    self.assertTrue(caches[0].samefile(linux_cache))
                    with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "xdg")}):
                        self.assertEqual(skills._tectonic_cache_paths(), [xdg_cache])
                with mock.patch.dict(os.environ, {"TECTONIC_CACHE_DIR": str(override)}):
                    self.assertEqual(skills._tectonic_cache_paths(), [override])

    def test_cache_environment_is_forwarded_only_for_trusted_report_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "tmp").mkdir()
            context = skills.SkillContext(root, policy_for("trimmer"))
            with mock.patch.dict(os.environ, {
                "HOME": str(root / "host-home"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "TECTONIC_CACHE_DIR": str(root / "tectonic"),
                "OPENAI_API_KEY": "should-not-reach-cas",
            }):
                cas = skills._clean_tool_environment(context)
                compiler = skills._clean_tool_environment(context, use_host_home=True)
            self.assertNotIn("XDG_CACHE_HOME", cas)
            self.assertNotIn("TECTONIC_CACHE_DIR", cas)
            self.assertNotIn("OPENAI_API_KEY", cas)
            self.assertNotIn("OPENAI_API_KEY", compiler)
            self.assertEqual(cas["HOME"], str(root / "tmp/tool-home"))
            self.assertEqual(compiler["XDG_CACHE_HOME"], str(root / "cache"))
            self.assertEqual(compiler["TECTONIC_CACHE_DIR"], str(root / "tectonic"))


if __name__ == "__main__":
    unittest.main()
