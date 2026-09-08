#!/usr/bin/env python3
"""Export an allowlisted GitHub source directory and ZIP, never local runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import zipfile

from check_distribution import audit_entries, audit_source_zip


ROOT_FILES = frozenset({
    "README.md", "README.zh-CN.md", "LICENSE", "Design.md", "IMPLEMENTATION.md",
    "pyproject.toml", "setup.py", "MANIFEST.in", ".gitignore", ".gitattributes",
})
SOURCE_DIRECTORIES = (
    "src", "tests", "examples", "evals", "docs", "scripts",
    ".agents/skills", ".github/workflows",
)
PUBLIC_SUFFIXES = frozenset({
    ".py", ".md", ".toml", ".json", ".html", ".css", ".js", ".txt",
    ".woff2", ".yml", ".yaml",
})
PRIVATE_NAMES = frozenset({
    "long_tests", "runs", "private", "projects", "__pycache__", "node_modules",
    "build", "dist", "release", "venv", "workspaces", "task-archive",
    "auth.json", "credentials.json", "token.json", "tokens.json",
})
MARKER = ".franta-release.json"


def public_files(root: Path) -> list[Path]:
    """Select only known public artifacts; reject source-tree symlinks."""
    selected = [root / name for name in ROOT_FILES if (root / name).is_file()]
    launcher = root / "bin/franta"
    if launcher.is_file():
        selected.append(launcher)
    for directory in SOURCE_DIRECTORIES:
        source = root / directory
        component_path = root
        for component in Path(directory).parts:
            component_path = component_path / component
            if component_path.is_symlink():
                raise ValueError(f"Refusing symlinked source directory: {component_path.relative_to(root)}")
        if not source.exists():
            continue
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if any(
                part in PRIVATE_NAMES or part.startswith(".") or part.endswith(".egg-info")
                or part.startswith(("credentials-", "service-account"))
                for part in relative.parts
            ):
                continue
            if path.is_symlink():
                # Repository discovery aliases point to independently packaged
                # Explorer skills. Export those canonical files only once.
                if (
                    directory == ".agents/skills" and len(relative.parts) == 1
                    and path.resolve() == root / "src/explorer_system/skills" / path.name
                ):
                    continue
                raise ValueError(f"Refusing symlinked source entry: {path.relative_to(root)}")
            if path.is_file() and path.suffix in PUBLIC_SUFFIXES:
                selected.append(path)
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Refusing symlinked source file: {path.relative_to(root)}")
    return sorted(set(selected), key=lambda item: item.relative_to(root).as_posix())


def export(root: Path, output: Path, *, force: bool = False) -> tuple[Path, Path, int]:
    root, output = root.resolve(), output.resolve()
    destination = output / "Franta"
    if any(destination.is_relative_to((root / directory).resolve()) for directory in SOURCE_DIRECTORIES):
        raise ValueError("Output must be outside public source directories; use release/ or a separate directory")
    files = public_files(root)
    required = {"pyproject.toml", "README.md", "LICENSE", "src/franta/__init__.py"}
    missing = required - {path.relative_to(root).as_posix() for path in files}
    if missing:
        raise ValueError(f"Missing required source files: {sorted(missing)}")
    audit_entries({path.relative_to(root).as_posix(): path.read_bytes() for path in files})
    if destination == root or destination in root.parents:
        raise ValueError("Output must not replace the source checkout")
    if destination.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    if destination.exists():
        marker = destination / MARKER
        if not force or not marker.is_file():
            raise ValueError("Export already exists; --force only replaces a marked Franta export")
        if json.loads(marker.read_text(encoding="utf-8")).get("generated_by") != "export_release.py":
            raise ValueError("Existing directory is not a recognized Franta export")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for path in files:
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    (destination / MARKER).write_text(
        json.dumps({"generated_by": "export_release.py", "source_files": len(files)}, indent=2) + "\n",
        encoding="utf-8",
    )
    archive = output / "Franta-source.zip"
    if archive.is_symlink():
        raise ValueError("Output archive must not be a symlink")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(output).as_posix())
    audit_source_zip(archive)
    return destination, archive, len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("release"))
    parser.add_argument("--force", action="store_true", help="replace an earlier marked export")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination, archive, count = export(root, args.output_dir, force=args.force)
    print(json.dumps({"directory": str(destination), "zip": str(archive), "files": count}, indent=2))


if __name__ == "__main__":
    main()
