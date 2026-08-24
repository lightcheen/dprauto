import unittest

from dprauto.adapters.python.test_matrix import (
    nox_session_metadata,
    preferred_matrix_name,
    tox_environment_metadata,
    tox_pytest_parallel_metadata,
)


class ToxMatrixTests(unittest.TestCase):
    def test_multiline_brace_envlist_excludes_quality_and_selects_runtime(self) -> None:
        environments = tox_environment_metadata(
            """[tox]
envlist =
    lint
    docs
    py{39,310,311}

[testenv]
commands = pytest tests

[testenv:lint]
commands = ruff check .
"""
        )
        by_name = {item["name"]: item for item in environments}

        self.assertEqual(
            tuple(by_name),
            ("lint", "docs", "py39", "py310", "py311"),
        )
        self.assertFalse(by_name["lint"]["safe"])
        self.assertFalse(by_name["docs"]["safe"])
        self.assertEqual(by_name["py311"]["python_version"], "3.11")
        self.assertEqual(
            preferred_matrix_name(environments, python_version="3.11"),
            "py311",
        )

    def test_expensive_and_special_environments_are_not_unit_tests(self) -> None:
        environments = tox_environment_metadata(
            """[tox]
envlist = py311, fuzz, run_self, generate_schema

[testenv]
commands = pytest tests

[testenv:fuzz]
commands = python scripts/fuzz.py

[testenv:run_self]
commands = black --check src tests
"""
        )
        by_name = {item["name"]: item for item in environments}

        self.assertTrue(by_name["py311"]["safe"])
        self.assertEqual(by_name["fuzz"]["kind"], "expensive")
        self.assertFalse(by_name["run_self"]["safe"])
        self.assertFalse(by_name["generate_schema"]["safe"])

    def test_tox4_env_list_and_pytest_parallel_factor_are_parsed(self) -> None:
        content = """[tox]
env_list = py{39,310,311}{,-parallel}

[testenv]
deps =
    parallel: pytest-xdist
commands =
    pytest
    parallel: --numprocesses auto
"""

        environments = tox_environment_metadata(content)
        metadata = tox_pytest_parallel_metadata(content)

        self.assertIn("py311-parallel", {item["name"] for item in environments})
        self.assertEqual(metadata["factor"], "parallel")
        self.assertEqual(metadata["dependency"], "pytest-xdist")
        self.assertEqual(metadata["requested_workers"], "auto")

    def test_parallel_metadata_requires_matching_env_dependency_and_argument(self) -> None:
        missing_dependency = """[tox]
env_list = py311{,-parallel}
[testenv]
commands =
    pytest
    parallel: --numprocesses auto
"""
        missing_argument = """[tox]
env_list = py311{,-parallel}
[testenv]
deps =
    parallel: pytest-xdist
commands = pytest
"""

        self.assertEqual(tox_pytest_parallel_metadata(missing_dependency), {})
        self.assertEqual(tox_pytest_parallel_metadata(missing_argument), {})

    def test_large_factor_matrix_keeps_later_runtime_representatives(self) -> None:
        environments = tox_environment_metadata(
            """[tox]
env_list = py{39,310,311,312,313}{,-compatibility,-coverage,-jsonschema}{,-devdeps}{,-parallel}{,-pytestdev}

[testenv]
commands = pytest
"""
        )
        names = {item["name"] for item in environments}

        self.assertIn("py311", names)
        self.assertIn("py311-parallel", names)


class NoxMatrixTests(unittest.TestCase):
    def test_sessions_preserve_python_versions_and_reject_parameterized_matrix(self) -> None:
        sessions = nox_session_metadata(
            '''import nox

@nox.session(name="tests", python=["3.10", "3.11"])
def unit(session):
    session.run("pytest", "tests")

@nox.session
@nox.parametrize("database", ["postgres", "mysql"])
def integration(session, database):
    session.run("pytest", "tests/integration")

@nox.session
def lint(session):
    session.run("ruff", "check", ".")
'''
        )
        by_name = {item["name"]: item for item in sessions}

        self.assertEqual(by_name["tests"]["python_versions"], ("3.10", "3.11"))
        self.assertTrue(by_name["tests"]["safe"])
        self.assertTrue(by_name["integration"]["parameterized"])
        self.assertFalse(by_name["integration"]["safe"])
        self.assertFalse(by_name["lint"]["safe"])
        self.assertEqual(
            preferred_matrix_name(sessions, python_version="3.11"),
            "tests",
        )


if __name__ == "__main__":
    unittest.main()
