import json
import os
import tempfile
import unittest
from pathlib import Path

from evaluations.corpus60.freeze_corpus import create_freeze, snapshot_identity, validate_freeze


class Corpus60FreezeTests(unittest.TestCase):
    def test_snapshot_identity_detects_content_and_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            source = root / "src" / "main.cpp"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            os.symlink("src/main.cpp", root / "main.cpp")
            first = snapshot_identity(root)

            source.write_text("int main() { return 1; }\n", encoding="utf-8")
            second = snapshot_identity(root)

            self.assertNotEqual(first["tree_sha256"], second["tree_sha256"])
            self.assertEqual(first["file_count"], 1)
            self.assertEqual(first["symlink_count"], 1)

    def test_freeze_is_immutable_and_detects_snapshot_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources" / "demo"
            source.mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\nname='demo'\n")
            cases = [
                {
                    "case_id": f"python-demo-{index}",
                    "repository": f"example/demo-{index}",
                    "revision": str(index).zfill(40),
                    "local_path": "sources/demo",
                }
                for index in range(60)
            ]
            manifest = {
                "suite_id": "demo-suite",
                "dprauto_revision": "a" * 40,
                "cases": cases,
            }
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "exclusions.json").write_text(json.dumps({"repositories": []}))
            probe = root / "probe-results.json"
            probe.write_text(
                json.dumps(
                    {
                        "suite_id": "demo-suite",
                        "dprauto_revision": "a" * 40,
                        "scope": "static",
                        "counts": {},
                    }
                )
            )

            create_freeze(root, definition_revision="b" * 40, probe_path=probe)
            self.assertEqual(validate_freeze(root, require_sources=True), [])
            with self.assertRaises(FileExistsError):
                create_freeze(root, definition_revision="b" * 40, probe_path=probe)

            (source / "pyproject.toml").write_text("[project]\nname='changed'\n")
            errors = validate_freeze(root, require_sources=True)
            self.assertTrue(any("tree_sha256 changed" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
