import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.multilang import MultiLanguageProjectParser
from dprauto.config import BuildConfig
from dprauto.domain.models import SourceReference
from dprauto.strategies import NativeTemplateStrategy


class ComponentAwareParserTests(unittest.TestCase):
    def parse(self, files: dict[str, str]):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        profile = MultiLanguageProjectParser().parse(SourceReference("fixture"), root)
        return root, profile

    def test_nested_native_component_beats_auxiliary_python_file(self) -> None:
        _, profile = self.parse(
            {
                "helper.py": "print('developer helper')\n",
                "src/Makefile": "engine: engine.cpp\n\t$(CXX) engine.cpp -o engine\n",
                "src/engine.cpp": "int main() { return 0; }\n",
            }
        )

        self.assertEqual(profile.metadata["component_root"], "src")
        self.assertEqual(profile.metadata["parser_registry_selection"], "native-rules-v1")
        self.assertEqual(profile.metadata["primary_build_system"], "make")
        self.assertEqual(profile.build_files, ("Makefile",))
        self.assertIn("C++", profile.languages)

    def test_native_component_excludes_repository_dockerfile_and_nested_binding(self) -> None:
        _, profile = self.parse(
            {
                "Dockerfile": "FROM debian:bookworm-slim\n",
                "src/CMakeLists.txt": "project(native)\nadd_library(native native.cpp)\n",
                "src/native.cpp": "void native() {}\n",
                "src/bindings/python/setup.py": "from setuptools import setup\n",
                "src/bindings/python/module.py": "VALUE = 1\n",
            }
        )

        candidates = profile.metadata["component_candidates"]
        self.assertEqual(profile.metadata["component_root"], "src")
        self.assertEqual(profile.dockerfiles, ())
        self.assertEqual(
            {candidate["root"] for candidate in candidates},
            {"src", "src/bindings/python"},
        )

    def test_documented_system_order_controls_native_plan(self) -> None:
        _, profile = self.parse(
            {
                "README.md": "$ meson setup build\n$ meson compile -C build\n",
                "CMakeLists.txt": "project(sample)\nadd_library(sample sample.cpp)\n",
                "meson.build": "project('sample', 'cpp')\nlibrary('sample', 'sample.cpp')\n",
                "sample.cpp": "void sample() {}\n",
            }
        )
        plan = NativeTemplateStrategy(None, BuildConfig()).create_plan(profile)  # type: ignore[arg-type]

        self.assertEqual(profile.metadata["primary_build_system"], "meson")
        self.assertEqual(profile.package_managers[:2], ("meson", "cmake"))
        self.assertEqual(plan.metadata["build_system"], "meson")
        self.assertIn("meson setup build", plan.generated_files[0].content)


if __name__ == "__main__":
    unittest.main()
