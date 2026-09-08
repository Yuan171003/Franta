"""Include the canonical worker skills in installed distributions.

The repository keeps these documents in .agents/skills so coding tools can also
discover them. Wheels need their own copy next to the installed Python modules.
All project metadata lives in pyproject.toml.
"""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSkills(build_py):
    def _skill_mapping(self) -> dict[str, str]:
        project = Path(__file__).resolve().parent
        source = project / ".agents" / "skills"
        if source.is_symlink() or source.parent.is_symlink():
            raise RuntimeError("Worker skill source directories must not be symlinks")
        files = sorted(source.rglob("*.md"))
        if not files:
            raise RuntimeError("The source distribution is missing .agents/skills")
        for path in source.rglob("*"):
            if path.is_symlink() and not (
                path.parent == source
                and path.resolve() == project / "src/explorer_system/skills" / path.name
            ):
                raise RuntimeError("Worker skill resources must not contain symlinks")
        destination = Path(self.build_lib) / "franta" / "skills"
        return {
            str(destination / path.relative_to(source)): str(path)
            for path in files
        }

    def build_package_data(self) -> None:
        super().build_package_data()
        for target, source in self._skill_mapping().items():
            self.mkpath(str(Path(target).parent))
            self.copy_file(source, target)

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        return list(dict.fromkeys([
            *super().get_outputs(include_bytecode),
            *self._skill_mapping(),
        ]))

    def get_output_mapping(self) -> dict[str, str]:
        return {**super().get_output_mapping(), **self._skill_mapping()}


setup(cmdclass={"build_py": BuildWithSkills})
