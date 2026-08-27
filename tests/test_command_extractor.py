import unittest

from dprauto.domain.enums import CommandPurpose
from dprauto.inspection.commands import CommandExtractor


class CommandExtractorTests(unittest.TestCase):
    def test_markdown_commands_are_classified_by_rule_and_context(self) -> None:
        markdown = """
# Example
## Installation
```bash
python -m pip install .
```
## Tests
$ pytest -q
## Start server
`uvicorn demo.main:app`
```python
import demo
```
"""
        commands = CommandExtractor().extract_markdown("README.md", markdown)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(by_text["python -m pip install ."], CommandPurpose.INSTALL)
        self.assertEqual(by_text["pytest -q"], CommandPurpose.TEST)
        self.assertEqual(by_text["uvicorn demo.main:app"], CommandPurpose.RUN)
        self.assertNotIn("import demo", by_text)

    def test_ci_run_blocks_and_single_line_steps_are_extracted(self) -> None:
        workflow = """
steps:
  - run: pip install -r requirements.txt
  - run: |
      python -m pytest -q
      ruff check .
"""
        commands = CommandExtractor().extract_ci(".github/workflows/ci.yml", workflow)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(by_text["pip install -r requirements.txt"], CommandPurpose.INSTALL)
        self.assertEqual(by_text["python -m pytest -q"], CommandPurpose.TEST)
        self.assertEqual(by_text["ruff check ."], CommandPurpose.OTHER)

    def test_ci_context_does_not_turn_every_command_into_a_test(self) -> None:
        workflow = """
steps:
  - run: pip install black pytest
  - run: |
      python generate_badges.py
      ruff check &&
      python -m pytest
"""
        commands = CommandExtractor().extract_ci(".github/workflows/ci.yml", workflow)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(by_text["pip install black pytest"], CommandPurpose.INSTALL)
        self.assertEqual(by_text["python generate_badges.py"], CommandPurpose.OTHER)
        self.assertEqual(by_text["ruff check"], CommandPurpose.OTHER)
        self.assertNotIn("ruff check &&", by_text)

    def test_ci_matrix_commands_are_expanded_to_bounded_literal_values(self) -> None:
        workflow = """
strategy:
  matrix:
    toxenv:
      - lint
      - py310
      - py311
    python-version: ["3.10", "3.11"]
steps:
  - run: tox -e ${{ matrix.toxenv }}
  - run: python${{ matrix.python-version }} -m pytest -q
"""
        commands = CommandExtractor().extract_ci(".github/workflows/tests.yml", workflow)
        texts = {command.text for command in commands}

        self.assertIn("tox -e lint", texts)
        self.assertIn("tox -e py311", texts)
        self.assertIn("python3.10 -m pytest -q", texts)
        self.assertIn("python3.11 -m pytest -q", texts)
        self.assertNotIn("tox -e ${{ matrix.toxenv }}", texts)

    def test_rst_indented_command_uses_section_context(self) -> None:
        readme = """
Usage
=====

Run it::

    python -m example --help
"""
        commands = CommandExtractor().extract_markdown("README.rst", readme)

        self.assertEqual(commands[0].text, "python -m example --help")
        self.assertEqual(commands[0].purpose, CommandPurpose.RUN)

    def test_multilang_build_and_test_commands_are_classified_semantically(self) -> None:
        workflow = """
steps:
  - run: mvn -B -DskipTests package
  - run: mvn -B test
  - run: ./gradlew assemble
  - run: ./gradlew :core:test
  - run: ./gradlew build -x test
  - run: cmake -S . -B build
  - run: cmake --build build
  - run: ctest --test-dir build
  - run: make check
  - run: meson setup build
  - run: meson test -C build
  - run: ninja test
"""

        commands = CommandExtractor().extract_ci(".github/workflows/ci.yml", workflow)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(by_text["mvn -B -DskipTests package"], CommandPurpose.BUILD)
        self.assertEqual(by_text["mvn -B test"], CommandPurpose.TEST)
        self.assertEqual(by_text["./gradlew assemble"], CommandPurpose.BUILD)
        self.assertEqual(by_text["./gradlew :core:test"], CommandPurpose.TEST)
        self.assertEqual(by_text["./gradlew build -x test"], CommandPurpose.BUILD)
        self.assertEqual(by_text["cmake -S . -B build"], CommandPurpose.BUILD)
        self.assertEqual(by_text["cmake --build build"], CommandPurpose.BUILD)
        self.assertEqual(by_text["ctest --test-dir build"], CommandPurpose.TEST)
        self.assertEqual(by_text["make check"], CommandPurpose.TEST)
        self.assertEqual(by_text["meson setup build"], CommandPurpose.BUILD)
        self.assertEqual(by_text["meson test -C build"], CommandPurpose.TEST)
        self.assertEqual(by_text["ninja test"], CommandPurpose.TEST)

    def test_special_ci_targets_cannot_masquerade_as_tests(self) -> None:
        workflow = """
steps:
  - run: pip download -r cryptography.txt
  - run: tox -e lint
  - run: nox -s docs
  - run: ./gradlew spotlessCheck
  - run: make fuzz
"""

        commands = CommandExtractor().extract_ci(".github/workflows/tests.yml", workflow)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(by_text["pip download -r cryptography.txt"], CommandPurpose.INSTALL)
        self.assertEqual(by_text["tox -e lint"], CommandPurpose.OTHER)
        self.assertEqual(by_text["nox -s docs"], CommandPurpose.OTHER)
        self.assertEqual(by_text["./gradlew spotlessCheck"], CommandPurpose.OTHER)
        self.assertEqual(by_text["make fuzz"], CommandPurpose.OTHER)

    def test_ci_container_and_run_named_helpers_are_not_application_entries(self) -> None:
        workflow = """
steps:
  - run: docker run --rm toolchain-image make test
  - run: bash ./p2996/run_docker.sh bash
  - run: ./linkandrun jsonexamples/twitter.json
"""

        commands = CommandExtractor().extract_ci(".github/workflows/ci.yml", workflow)
        by_text = {command.text: command.purpose for command in commands}

        self.assertEqual(
            by_text["docker run --rm toolchain-image make test"],
            CommandPurpose.OTHER,
        )
        self.assertEqual(
            by_text["bash ./p2996/run_docker.sh bash"],
            CommandPurpose.OTHER,
        )
        self.assertEqual(
            by_text["./linkandrun jsonexamples/twitter.json"],
            CommandPurpose.OTHER,
        )


if __name__ == "__main__":
    unittest.main()
