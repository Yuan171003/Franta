#!/usr/bin/env python3
"""Audit a wheel and sdist, then install and smoke-test outside the checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


PERSONAL_PATH = re.compile(rb"(?:/Users/|/home/)[A-Za-z0-9_.-]+/|[A-Z]:\\Users\\[A-Za-z0-9_.-]+\\")
FORBIDDEN_PARTS = {
    "long_tests", "private", "workspaces", "task-archive", "__pycache__",
    ".DS_Store", ".git", ".codex", ".venv", ".pytest_cache",
}
CREDENTIAL_FILES = {"auth.json", "credentials.json", "token.json", "tokens.json"}
LEGACY_NAME = b"da" + b"nus"
CREDENTIAL_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9_-])(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    rb"github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def audit_entries(entries: dict[str, bytes]) -> None:
    for name, data in entries.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe archive member: {name}")
        if FORBIDDEN_PARTS.intersection(path.parts):
            raise ValueError(f"Private/cache file in distribution: {name}")
        if path.name.startswith(".env") or path.suffix in {".pyc", ".pyo", ".pem", ".key", ".sqlite3", ".db"}:
            raise ValueError(f"Credential, runtime data, or cache in distribution: {name}")
        if path.name in CREDENTIAL_FILES or path.name.startswith(("credentials-", "service-account")):
            raise ValueError(f"Credential file in distribution: {name}")
        if len(data) >= 100_000_000:
            raise ValueError(f"File exceeds the GitHub source-file size limit: {name}")
        if LEGACY_NAME in name.lower().encode() or LEGACY_NAME in data.lower():
            raise ValueError(f"Retired project name in distribution: {name}")
        if PERSONAL_PATH.search(data):
            raise ValueError(f"Absolute personal home path in distribution: {name}")
        if CREDENTIAL_TOKEN.search(data):
            raise ValueError(f"Possible credential in distribution: {name}")


def audit_source_zip(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    audit_entries(entries)
    return len(entries)


def check_resources(root: Path, wheel: dict[str, bytes], source: dict[str, bytes]) -> None:
    audit_entries(wheel)
    audit_entries(source)
    resources = []
    for source_root, package_root in (
        (root / ".agents/skills", "franta/skills"),
        (root / "src/advisor_system/skills", "advisor_system/skills"),
        (root / "src/explorer_system/skills", "explorer_system/skills"),
        (root / "src/dashboard_system/static", "dashboard_system/static"),
    ):
        for path in source_root.rglob("*"):
            if path.is_file() and path.name != ".DS_Store":
                relative = path.relative_to(source_root).as_posix()
                resources.append((f"{package_root}/{relative}", path))
    for name, path in resources:
        if wheel.get(name) != path.read_bytes():
            raise ValueError(f"Missing or stale wheel resource: {name}")
        source_name = path.relative_to(root).as_posix()
        if source.get(source_name) != path.read_bytes():
            raise ValueError(f"Missing or stale source resource: {source_name}")
    for name in ("README.md", "LICENSE", "pyproject.toml", "setup.py", "MANIFEST.in", "scripts/check_distribution.py"):
        if name not in source:
            raise ValueError(f"Required source file missing: {name}")
    print(f"Archive audit passed: {len(resources)} resources, including worker skills and vendor licenses")


INSTALLED_SMOKE = r'''
from importlib.resources import files
from pathlib import Path
import franta
from franta.access import policy_for
from franta.materialize import WorkspaceMaterializer
from franta.advisor_adapter import advisor_skill_source
from franta.explorer_adapter import explorer_skill_source

materializer = WorkspaceMaterializer(
    Path.cwd() / "workspaces",
    additional_skill_sources=(advisor_skill_source(), explorer_skill_source()),
)
assert materializer.skill_source == Path(franta.__file__).resolve().parent / "skills"
names = sorted({p.name for source in materializer.skill_sources for p in source.iterdir() if p.is_dir()})
assert {"CAS", "internal-search", "record-progress", "record-summary", "selection-report"} <= set(names)
workspace = materializer.create(
    "installed-wheel", root_problem="Prove that 1 + 1 = 2.",
    policy=policy_for("worker", mode="research"), skills=names,
)
for name in names:
    assert (workspace.path / ".agents/skills" / name / "SKILL.md").read_text()
assert (workspace.path / ".agents/skills/record-progress/references/payload.md").read_text()
assets = files("dashboard_system").joinpath("static")
for name in ("index.html", "app.js", "style.css", "vendor/katex.js", "vendor/katex.min.css", "vendor/LICENSE-katex.txt", "vendor/NOTICE.md"):
    assert assets.joinpath(name).read_bytes(), name
assert len(list(assets.joinpath("vendor/fonts").iterdir())) >= 20
print(f"Installed wheel smoke passed: {len(names)} materialized skills and dashboard assets")
'''


def check_install(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="franta-wheel-check-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        clean_environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        clean_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        for command in (
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            [str(python), "-I", "-c", INSTALLED_SMOKE],
            [str(scripts / ("franta.exe" if os.name == "nt" else "franta")), "--help"],
        ):
            subprocess.run(command, cwd=root, env=clean_environment, check=True, timeout=120)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, nargs="?", default=Path("dist"))
    parser.add_argument("--source-zip", type=Path, help="also audit an exported GitHub source ZIP")
    args = parser.parse_args()
    wheels = list(args.dist.glob("*.whl"))
    sources = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("expected exactly one wheel and one source archive in the distribution directory")
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    with tarfile.open(sources[0], "r:gz") as archive:
        source = {}
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Unexpected archive link: {member.name}")
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                source[PurePosixPath(member.name).relative_to(PurePosixPath(member.name).parts[0]).as_posix()] = handle.read()
    check_resources(Path(__file__).resolve().parents[1], wheel, source)
    check_install(wheels[0].resolve())
    if args.source_zip is not None:
        print(f"Source ZIP audit passed: {audit_source_zip(args.source_zip)} files")


if __name__ == "__main__":
    main()
