import tempfile
import unittest
from pathlib import Path

from dprauto.domain.workspace import (
    BuildMarker,
    BuildMarkerIndex,
    ComponentCandidate,
    ComponentEvidence,
    ComponentGraph,
    ComponentRelation,
    RepositoryScan,
)
from dprauto.errors import ModelValidationError
from dprauto.inspection.scanner import FileScanner


class WorkspaceModelTests(unittest.TestCase):
    def test_repository_scan_subtree_rebases_indexed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "component").mkdir()
            (root / "component/CMakeLists.txt").write_text("project(sample)\n")
            (root / "README.md").write_text("sample\n")
            scan = FileScanner().scan(root)

            subtree = scan.subtree("component")

            self.assertEqual(subtree.root, (root / "component").resolve())
            self.assertEqual(subtree.files, ("CMakeLists.txt",))
            self.assertEqual(subtree.read_text("CMakeLists.txt"), "project(sample)\n")

    def test_repository_scan_subtree_rejects_escape_and_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scan = FileScanner().scan(Path(directory))
            with self.assertRaises(ModelValidationError):
                scan.subtree("../outside")
            with self.assertRaises(ModelValidationError):
                scan.subtree("missing")

    def test_build_marker_index_is_deterministic_and_queryable(self) -> None:
        marker = BuildMarker("src/CMakeLists.txt", "src", "cmake")
        index = BuildMarkerIndex((marker,))
        self.assertEqual(index.roots, ("src",))
        self.assertEqual(index.for_root("src"), (marker,))
        with self.assertRaises(ModelValidationError):
            BuildMarkerIndex((marker, marker))

    def test_component_graph_ranks_without_closed_role_or_relation_enums(self) -> None:
        root = ComponentCandidate(
            "root",
            ".",
            ("python-packaging",),
            ("pyproject.toml",),
            role="support-tool",
            score=5,
        )
        native = ComponentCandidate(
            "native",
            "src",
            ("cmake",),
            ("src/CMakeLists.txt",),
            role="runtime",
            evidence=(ComponentEvidence("ci", ".github/workflows/build.yml", weight=20),),
            score=20,
        )
        graph = ComponentGraph(
            (root, native),
            (ComponentRelation("root", "native", "binding_of"),),
            primary_component_id="native",
        )

        self.assertEqual(graph.primary, native)
        self.assertEqual(graph.ranked(), (native, root))

    def test_component_graph_rejects_unknown_or_ineligible_primary(self) -> None:
        component = ComponentCandidate(
            "vendor",
            "vendor/library",
            ("cmake",),
            ("vendor/library/CMakeLists.txt",),
            primary_eligible=False,
        )
        with self.assertRaises(ModelValidationError):
            ComponentGraph((component,), primary_component_id="missing")
        with self.assertRaises(ModelValidationError):
            ComponentGraph((component,), primary_component_id="vendor")

    def test_repository_scan_rejects_unsorted_or_unsafe_paths(self) -> None:
        with self.assertRaises(ModelValidationError):
            RepositoryScan(Path("/tmp/project"), ("z", "a"))
        with self.assertRaises(ModelValidationError):
            RepositoryScan(Path("/tmp/project"), ("../secret",))


if __name__ == "__main__":
    unittest.main()
