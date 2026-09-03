import tempfile
import unittest
from pathlib import Path

from dprauto.inspection.components import EvidenceRankedComponentDiscoverer
from dprauto.inspection.scanner import FileScanner


class ComponentDiscoveryTests(unittest.TestCase):
    def discover(self, files: dict[str, str]):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return EvidenceRankedComponentDiscoverer().discover(FileScanner().scan(root))

    def test_documented_nested_workspace_beats_sibling_build_roots(self) -> None:
        graph = self.discover(
            {
                "README.md": "Build it:\n$ cd runtime\n$ ./configure\n$ make\n",
                "runtime/configure": "#!/bin/sh\n",
                "runtime/main.cpp": "int main() {}\n",
                "helper/configure": "#!/bin/sh\n",
                "helper/tool.cpp": "void tool() {}\n",
            }
        )

        self.assertEqual(graph.primary.root, "runtime")
        self.assertEqual({item.root for item in graph.candidates}, {"helper", "runtime"})

    def test_vendor_candidate_is_retained_but_never_primary(self) -> None:
        graph = self.discover(
            {
                "src/CMakeLists.txt": "project(runtime)\n",
                "src/main.cpp": "int main() {}\n",
                "vendor/library/CMakeLists.txt": "project(vendor)\n",
                "vendor/library/library.cpp": "void library() {}\n",
            }
        )

        vendor = next(item for item in graph.candidates if item.root == "vendor/library")
        self.assertEqual(vendor.role, "vendor")
        self.assertFalse(vendor.primary_eligible)
        self.assertEqual(graph.primary.root, "src")

    def test_binding_is_preserved_without_stealing_native_primary(self) -> None:
        graph = self.discover(
            {
                "src/CMakeLists.txt": "project(native)\n",
                "src/native.cpp": "void native() {}\n",
                "src/bindings/python/setup.py": "from setuptools import setup\n",
                "src/bindings/python/module.py": "VALUE = 1\n",
            }
        )

        binding = next(item for item in graph.candidates if item.role == "binding")
        self.assertEqual(binding.root, "src/bindings/python")
        self.assertEqual(graph.primary.root, "src")
        self.assertTrue(
            any(
                relation.relation_type == "contains"
                and relation.source_id == graph.primary.component_id
                and relation.target_id == binding.component_id
                for relation in graph.relations
            )
        )

    def test_documented_meson_pipeline_orders_alternative_systems(self) -> None:
        graph = self.discover(
            {
                "README.md": (
                    "Building from source:\n$ meson setup build\n$ meson compile -C build\n"
                ),
                "CMakeLists.txt": "project(sample)\n",
                "meson.build": "project('sample', 'cpp')\n",
                "main.cpp": "int main() {}\n",
            }
        )

        self.assertEqual(graph.primary.root, ".")
        self.assertEqual(graph.primary.build_systems, ("meson", "cmake"))
        self.assertEqual(graph.primary.build_entries, ("meson.build", "CMakeLists.txt"))

    def test_nested_cmake_and_meson_fragments_are_not_standalone_components(self) -> None:
        graph = self.discover(
            {
                "CMakeLists.txt": "project(sample)\nadd_subdirectory(src)\n",
                "meson.build": "project('sample', 'cpp')\nsubdir('src')\n",
                "src/CMakeLists.txt": "add_library(sample sample.cpp)\n",
                "src/meson.build": "library('sample', 'sample.cpp')\n",
                "src/sample.cpp": "void sample() {}\n",
            }
        )

        self.assertEqual(tuple(item.root for item in graph.candidates), (".",))

    def test_editable_install_does_not_misclassify_pyproject_as_setuptools(self) -> None:
        graph = self.discover(
            {
                "README.md": "$ pip install -e .\n",
                "pyproject.toml": "[build-system]\nrequires = ['setuptools']\n",
                "setup.py": "from setuptools import setup\n",
                "package/__init__.py": "\n",
            }
        )

        self.assertEqual(graph.primary.build_systems[0], "python-packaging")

    def test_autotools_subdirectory_declaration_creates_dependency_relation(self) -> None:
        graph = self.discover(
            {
                "README.md": "$ cd runtime\n$ ./configure\n",
                "runtime/configure": "#!/bin/sh\n",
                "runtime/configure.ac": "AC_CONFIG_SUBDIRS([../library])\n",
                "runtime/main.cpp": "int main() {}\n",
                "library/configure": "#!/bin/sh\n",
                "library/configure.ac": "AC_INIT([library], [1])\n",
                "library/lib.cpp": "void library() {}\n",
            }
        )

        roots = {item.component_id: item.root for item in graph.candidates}
        dependencies = [
            relation
            for relation in graph.relations
            if relation.relation_type == "depends_on"
        ]
        self.assertEqual(len(dependencies), 1)
        self.assertEqual(roots[dependencies[0].source_id], "runtime")
        self.assertEqual(roots[dependencies[0].target_id], "library")

    def test_discovery_is_deterministic(self) -> None:
        files = {
            "pom.xml": "<project />\n",
            "module/pom.xml": "<project />\n",
            "module/src/main/java/App.java": "class App {}\n",
        }
        first = self.discover(files)
        second = self.discover(files)
        self.assertEqual(first, second)

    def test_root_build_consolidates_ordinary_nested_modules(self) -> None:
        graph = self.discover(
            {
                "pom.xml": "<project />\n",
                "module/pom.xml": "<project />\n",
                "module/src/main/java/App.java": "class App {}\n",
            }
        )

        self.assertEqual(tuple(item.root for item in graph.candidates), (".",))


if __name__ == "__main__":
    unittest.main()
