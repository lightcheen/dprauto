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
        self.assertEqual(by_text["ruff check ."], CommandPurpose.TEST)

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
        self.assertEqual(by_text["ruff check"], CommandPurpose.TEST)
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


if __name__ == "__main__":
    unittest.main()
