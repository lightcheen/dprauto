import tempfile
import unittest
from pathlib import Path

from dprauto.domain.models import ProjectProfile, SourceReference
from dprauto.verification.dependencies import TestDependencyPlanner


class TestDependencyPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "tests" / "helpers").mkdir(parents=True)
        (self.workspace / "sample").mkdir()
        (self.workspace / "sample" / "__init__.py").write_text("", encoding="utf-8")
        (self.workspace / "sample" / "core.py").write_text(
            "import yaml\nVALUE = 1\n", encoding="utf-8"
        )
        (self.workspace / "requirements-dev.txt").write_text(
            """setuptools==70.0.0
pytest==8.2.1
respx==0.21.1
pytest-git==1.7.0
pytest-env==1.1.3
pytest-mock==3.14.0
fiftyone==0.23.8
datasets==2.19.1
""",
            encoding="utf-8",
        )
        (self.workspace / "pytest.ini").write_text(
            "[pytest]\nenv =\n    TOKEN=value\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "conftest.py").write_text(
            "import pytest_git\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_good.py").write_text(
            "import pytest\nfrom sample.core import VALUE\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "helpers" / "mock_api.py").write_text(
            "import respx\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_helper.py").write_text(
            "from tests.helpers.mock_api import respx\n", encoding="utf-8"
        )
        (self.workspace / "tests" / "test_optional.py").write_text(
            """from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import fiftyone
def test_lazy():
    import datasets
""",
            encoding="utf-8",
        )
        (self.workspace / "tests" / "test_undeclared.py").write_text(
            "import starlette\n", encoding="utf-8"
        )

    def profile(self) -> ProjectProfile:
        files = (
            "tests/test_good.py",
            "tests/test_helper.py",
            "tests/test_optional.py",
            "tests/test_undeclared.py",
        )
        return ProjectProfile(
            "fixture",
            SourceReference("fixture://closure"),
            package_managers=("pip",),
            dependency_files=("requirements-dev.txt", "setup.py"),
            metadata={
                "safe_test_files": files,
                "runtime_dependency_names": ("pyyaml",),
            },
        )

    def test_reselects_declared_slice_and_excludes_heavy_unneeded_packages(self) -> None:
        plan = TestDependencyPlanner(max_test_files=3).plan(
            self.profile(),
            self.workspace,
            ("tests/test_good.py", "tests/test_undeclared.py"),
            command="python -m pytest tests/test_good.py tests/test_undeclared.py",
        )

        self.assertTrue(plan.applied)
        self.assertNotIn("tests/test_undeclared.py", plan.targets)
        self.assertEqual(
            plan.selected_requirements,
            (
                "pytest==8.2.1",
                "respx==0.21.1",
                "pytest-git==1.7.0",
                "pytest-env==1.1.3",
            ),
        )
        command = plan.install_commands[0]
        self.assertNotIn("fiftyone", command)
        self.assertNotIn("datasets", command)
        self.assertTrue(any("starlette" in item for item in plan.excluded_targets))
        self.assertEqual(plan.required_executables, ("git",))

    def test_type_checking_and_function_scope_imports_are_not_collection_dependencies(self) -> None:
        plan = TestDependencyPlanner(max_test_files=1).plan(
            self.profile(),
            self.workspace,
            ("tests/test_optional.py",),
            command="pytest tests/test_optional.py",
        )

        self.assertTrue(plan.applied)
        self.assertNotIn("fiftyone", plan.import_roots)
        self.assertNotIn("datasets", plan.import_roots)
        self.assertEqual(
            plan.selected_requirements,
            ("pytest==8.2.1", "pytest-git==1.7.0", "pytest-env==1.1.3"),
        )

    def test_hash_or_include_requirements_fall_back_to_whole_file(self) -> None:
        (self.workspace / "requirements-dev.txt").write_text(
            "pytest==8.2.1 --hash=sha256:abc\n", encoding="utf-8"
        )

        plan = TestDependencyPlanner().plan(
            self.profile(),
            self.workspace,
            ("tests/test_good.py",),
            command="pytest tests/test_good.py",
        )

        self.assertFalse(plan.applied)
        self.assertIn("hashes", plan.reason)


if __name__ == "__main__":
    unittest.main()
