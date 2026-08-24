import tempfile
import unittest
from pathlib import Path

from dprauto.errors import ProjectParsingError
from dprauto.inspection.scanner import FileScanner


class FileScannerTests(unittest.TestCase):
    def test_scan_is_sorted_and_ignores_generated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package").mkdir()
            (root / "package" / "module.py").write_text("value = 1")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("ignored")
            (root / ".venv").mkdir()
            (root / ".venv" / "hidden.py").write_text("ignored")
            (root / "README.md").write_text("read me")

            scanned = FileScanner().scan(root)

            self.assertEqual(scanned.files, ("README.md", "package/module.py"))
            self.assertEqual(scanned.read_text("README.md"), "read me")
            self.assertEqual(scanned.read_text("../outside"), "")

    def test_missing_workspace_raises_project_parsing_error(self) -> None:
        with self.assertRaises(ProjectParsingError):
            FileScanner().scan(Path("/path/that/does/not/exist"))


if __name__ == "__main__":
    unittest.main()
