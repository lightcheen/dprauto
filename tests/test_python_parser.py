import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.python import PythonProjectParser
from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import SourceReference
from dprauto.ports.parser import ProjectParser


FIXTURES = Path(__file__).parent / "fixtures" / "python"


def command_texts(profile, purpose):
    return {
        command.command.display
        for command in profile.commands
        if command.command.purpose is purpose
    }


class PythonProjectParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = PythonProjectParser()

    def parse(self, fixture: str):
        return self.parser.parse(
            SourceReference(f"fixture://{fixture}", revision="fixture-revision"),
            FIXTURES / fixture,
        )

    def test_implements_project_parser_port(self) -> None:
        self.assertIsInstance(self.parser, ProjectParser)

    def test_pyproject_library(self) -> None:
        profile = self.parse("pyproject_library")

        self.assertEqual(profile.project_id, "profile-lib@fixture-revision")
        self.assertTrue(profile.metadata["project_name_declared"])
        self.assertEqual(profile.languages, ("Python",))
        self.assertEqual(profile.project_type, ProjectType.LIBRARY)
        self.assertEqual(profile.runtime_constraints["python"], ">=3.10")
        self.assertEqual(profile.package_managers, ("pip",))
        self.assertIn("pyproject.toml", profile.dependency_files)
        self.assertIn("README.md", profile.readme_files)
        self.assertIn(".github/workflows/tests.yml", profile.ci_files)
        self.assertIn("python -m pip install .", command_texts(profile, CommandPurpose.INSTALL))
        self.assertIn("python -m pytest -q", command_texts(profile, CommandPurpose.TEST))

    def test_setup_cli(self) -> None:
        profile = self.parse("setup_cli")

        self.assertEqual(profile.project_type, ProjectType.CLI)
        self.assertEqual(profile.runtime_constraints["python"], ">=3.9")
        self.assertIn("requirements.txt", profile.dependency_files)
        self.assertIn("setup.py", profile.build_files)
        self.assertIn("profile-cli", command_texts(profile, CommandPurpose.RUN))
        self.assertIn(
            "python -m profile_cli --help",
            command_texts(profile, CommandPurpose.RUN),
        )
        self.assertEqual(profile.metadata["entry_points"], ("profile-cli",))

    def test_same_command_text_is_preserved_for_distinct_purposes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture").mkdir()
            (root / "fixture" / "__init__.py").write_text("", encoding="utf-8")
            (root / "fixture" / "__main__.py").write_text(
                'print("fixture output")\n', encoding="utf-8"
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                """# Fixture

## Installing

```sh
python -m fixture
```
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://dual-purpose"), root)

        self.assertEqual(profile.project_type, ProjectType.CLI)
        self.assertIn("python -m fixture", command_texts(profile, CommandPurpose.INSTALL))
        self.assertIn("python -m fixture", command_texts(profile, CommandPurpose.RUN))
        run_command = next(
            item
            for item in profile.commands
            if item.command.purpose is CommandPurpose.RUN
            and item.command.display == "python -m fixture"
        )
        self.assertEqual(run_command.source, "inferred:__main__.py")

    def test_poetry_web(self) -> None:
        profile = self.parse("poetry_web")

        self.assertEqual(profile.project_type, ProjectType.WEB)
        self.assertEqual(profile.runtime_constraints["python"], "^3.11")
        self.assertEqual(profile.package_managers, ("poetry",))
        self.assertEqual(profile.dockerfiles, ("Dockerfile",))
        self.assertIn("poetry install", command_texts(profile, CommandPurpose.INSTALL))
        self.assertIn(
            "uvicorn profile_web.main:app",
            command_texts(profile, CommandPurpose.RUN),
        )

    def test_requirements_script(self) -> None:
        profile = self.parse("requirements_script")

        self.assertEqual(profile.project_type, ProjectType.SCRIPT)
        self.assertEqual(profile.runtime_constraints["python"], "3.12")
        self.assertEqual(profile.package_managers, ("pip",))
        self.assertEqual(profile.dockerfiles, ("Dockerfile-debug",))
        self.assertIn(
            "python -m pip install -r requirements.txt",
            command_texts(profile, CommandPurpose.INSTALL),
        )
        self.assertIn("python src/main.py", command_texts(profile, CommandPurpose.RUN))
        self.assertIn(
            "python -m unittest discover",
            command_texts(profile, CommandPurpose.TEST),
        )

    def test_dev_extras_setup_script_and_non_ci_dockerfile_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".github" / "actions" / "helper").mkdir(parents=True)
            (root / "deploy").mkdir()
            (root / ".github" / "actions" / "helper" / "Dockerfile").write_text(
                "FROM python:3.11\n", encoding="utf-8"
            )
            (root / "deploy" / "Dockerfile.prod").write_text(
                "FROM python:3.11\n", encoding="utf-8"
            )
            (root / "setup.sh").write_text("python -m pip install .\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                """[project]
name = "fixture"
requires-python = ">=3.8"

[project.optional-dependencies]
test = ["pytest"]
docs = ["sphinx"]

[tool.poetry.group.dev.dependencies]
ruff = "*"
""",
                encoding="utf-8",
            )
            (root / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")

            profile = self.parser.parse(SourceReference("fixture://generic"), root)

        self.assertEqual(profile.dockerfiles, ("deploy/Dockerfile.prod",))
        self.assertIn("setup.sh", profile.build_files)
        self.assertEqual(profile.metadata["test_dependency_groups"], ("test", "dev"))
        self.assertEqual(profile.metadata["test_dependency_extras"], ("test",))
        self.assertEqual(profile.metadata["test_dependency_manager_groups"], ("dev",))

    def test_pytest_semantic_extra_and_bounded_test_evidence_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests" / "integration").mkdir(parents=True)
            (root / "pyproject.toml").write_text(
                """[project]
name = "fixture"

[project.optional-dependencies]
pytesting = [
    "fixture[full]",
    "pytest>=7",
    "pytest-cov",
]
coveraging = ["coverage"]
docs = ["sphinx"]
""",
                encoding="utf-8",
            )
            (root / "tests" / "test_unit.py").write_text(
                "def test_ok(): pass\n", encoding="utf-8"
            )
            (root / "tests" / "test_remote.py").write_text(
                'client.download("https://example.invalid/value")\n',
                encoding="utf-8",
            )
            (root / "tests" / "integration" / "test_api.py").write_text(
                "def test_api(): pass\n", encoding="utf-8"
            )
            (root / "tests" / "conftest.py").write_text(
                """import os

class Settings:
    TOKEN = os.environ["SERVICE_TOKEN"]

def optional_lookup():
    return os.environ["FUNCTION_ONLY_TOKEN"]
""",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://test-evidence"), root)

        self.assertEqual(profile.metadata["test_dependency_extras"], ("pytesting",))
        self.assertEqual(profile.metadata["test_file_count"], 3)
        self.assertEqual(profile.metadata["safe_test_files"], ("tests/test_unit.py",))
        self.assertEqual(
            profile.metadata["external_test_files"],
            ("tests/integration/test_api.py", "tests/test_remote.py"),
        )
        self.assertEqual(
            profile.metadata["test_required_environment_variables"],
            ("SERVICE_TOKEN",),
        )

    def test_tox_default_environment_is_inferred_without_full_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tox.ini").write_text(
                """[tox]
envlist =
    lint
    docs
    py{310,311}

[testenv]
commands = pytest

[testenv:lint]
commands = ruff check .
""",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"fixture\"\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_basic.py").write_text("def test_ok(): pass\n", encoding="utf-8")

            profile = self.parser.parse(SourceReference("fixture://tox"), root)

        self.assertEqual(profile.metadata["default_tox_env"], "py310")
        self.assertIn("tox -e py310", command_texts(profile, CommandPurpose.TEST))
        environments = {
            item["name"]: item for item in profile.metadata["tox_environments"]
        }
        self.assertFalse(environments["lint"]["safe"])
        self.assertTrue(environments["py311"]["safe"])

    def test_pytest_parallelism_requires_tox_declaration_and_ci_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(
                """jobs:
  test:
    with:
      envs: |
        - linux: py311-parallel
""",
                encoding="utf-8",
            )
            (root / "tox.ini").write_text(
                """[tox]
env_list = py{310,311}{,-parallel}

[testenv]
deps =
    parallel: pytest-xdist
commands =
    pytest
    parallel: --numprocesses auto
""",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"fixture\"\n", encoding="utf-8"
            )

            profile = self.parser.parse(SourceReference("fixture://parallel"), root)

        self.assertEqual(profile.metadata["pytest_parallel"]["factor"], "parallel")
        self.assertTrue(profile.metadata["pytest_parallel"]["ci_confirmed"])

    def test_system_dependency_hints_require_explicit_manifest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                """[project]
name = "native-fixture"
dependencies = ["pyscard>=2"]
""",
                encoding="utf-8",
            )
            (root / "requirements.txt").write_text(
                "-e git+https://github.com/example/dependency.git@main#egg=dependency\n",
                encoding="utf-8",
            )
            (root / "native_fixture.py").write_text("VALUE = 1\n", encoding="utf-8")

            profile = self.parser.parse(SourceReference("fixture://native"), root)

        self.assertEqual(
            profile.metadata["system_dependency_hints"],
            ("git-vcs", "pyscard-native"),
        )

    def test_setuptools_scm_requires_explicit_configuration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                """[build-system]
requires = ["setuptools", "setuptools_scm[toml]"]

[project]
name = "scm-fixture"
dynamic = ["version"]

[tool.setuptools_scm]
version_file = "src/scm_fixture/_version.py"
""",
                encoding="utf-8",
            )
            profile = self.parser.parse(SourceReference("fixture://scm"), root)

        self.assertEqual(
            profile.metadata["scm_versioning"],
            {
                "provider": "setuptools-scm",
                "evidence": "pyproject.toml:[tool.setuptools_scm]",
            },
        )

    def test_setuptools_scm_build_dependency_alone_is_not_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                """[build-system]
requires = ["setuptools", "setuptools_scm[toml]"]

[project]
name = "ordinary-fixture"
version = "1.0"
""",
                encoding="utf-8",
            )
            profile = self.parser.parse(SourceReference("fixture://ordinary"), root)

        self.assertEqual(profile.metadata["scm_versioning"], {})

    def test_empty_setuptools_scm_table_is_explicit_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                """[project]
name = "scm-fixture"
dynamic = ["version"]

[tool.setuptools_scm] # defaults are intentional
""",
                encoding="utf-8",
            )
            profile = self.parser.parse(SourceReference("fixture://scm-defaults"), root)

        self.assertEqual(
            profile.metadata["scm_versioning"]["provider"],
            "setuptools-scm",
        )

    def test_setuptools_scm_prefers_generated_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src" / "fixture").mkdir(parents=True)
            (root / "src" / "fixture" / "_version.py").write_text(
                "__version__ = version = '2.4.1.dev3+gabcdef0'\n",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                """[project]
name = "fixture"
dynamic = ["version"]

[tool.setuptools_scm]
version_file = "src/fixture/_version.py"
""",
                encoding="utf-8",
            )
            (root / "CHANGES.rst").write_text(
                "9.0.0 (unreleased)\n------------------\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://generated"), root)

        self.assertEqual(
            profile.metadata["scm_versioning"],
            {
                "provider": "setuptools-scm",
                "evidence": "pyproject.toml:[tool.setuptools_scm]",
                "version": "2.4.1.dev3+gabcdef0",
                "version_kind": "generated-file",
                "version_source": "src/fixture/_version.py",
            },
        )

    def test_setuptools_scm_uses_first_unreleased_changelog_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                """[project]
name = "fixture"
dynamic = ["version"]

[tool.setuptools_scm]
""",
                encoding="utf-8",
            )
            (root / "CHANGES.rst").write_text(
                "3.3.0 (unreleased)\n------------------\n\n3.2.0 (2024-04-05)\n",
                encoding="utf-8",
            )

            profile = self.parser.parse(SourceReference("fixture://changelog"), root)

        self.assertEqual(profile.metadata["scm_versioning"]["version"], "3.3.0")
        self.assertEqual(
            profile.metadata["scm_versioning"]["version_kind"],
            "unreleased-changelog",
        )
        self.assertEqual(
            profile.metadata["scm_versioning"]["version_source"],
            "CHANGES.rst:first-version-heading",
        )

    def test_setuptools_scm_does_not_use_later_or_documentation_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            (root / "pyproject.toml").write_text(
                """[project]
name = "fixture"
dynamic = ["version"]

[tool.setuptools_scm]
""",
                encoding="utf-8",
            )
            (root / "CHANGES.rst").write_text(
                "2.0.0 (2024-01-01)\n------------------\n\n9.0.0 (unreleased)\n",
                encoding="utf-8",
            )
            (root / "docs" / "CHANGELOG.md").write_text(
                "# 10.0.0 (unreleased)\n", encoding="utf-8"
            )

            profile = self.parser.parse(SourceReference("fixture://released"), root)

        self.assertNotIn("version", profile.metadata["scm_versioning"])

    def test_nox_default_session_is_inferred_without_docs_or_lint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "noxfile.py").write_text(
                """import nox

@nox.session
def lint(session):
    session.run("ruff", "check", ".")

@nox.session
def tests(session):
    session.run("pytest")
""",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"fixture\"\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_basic.py").write_text("def test_ok(): pass\n", encoding="utf-8")

            profile = self.parser.parse(SourceReference("fixture://nox"), root)

        self.assertEqual(profile.metadata["default_nox_session"], "tests")
        self.assertIn("nox -s tests", command_texts(profile, CommandPurpose.TEST))


if __name__ == "__main__":
    unittest.main()
