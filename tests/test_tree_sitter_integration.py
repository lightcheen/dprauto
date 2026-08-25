import importlib.util
import unittest
import warnings
from pathlib import Path

from dprauto.adapters.intelligence import TreeSitterSyntaxParser
from dprauto.domain.models import SourceReference
from dprauto.intelligence import KnowledgeNodeKind
from dprauto.intelligence.builder import (
    KnowledgeGraphBuildConfig,
    RepositoryKnowledgeGraphBuilder,
)


TREE_SITTER_AVAILABLE = importlib.util.find_spec("tree_sitter_languages") is not None
SOURCE_ROOT = Path("/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos")


@unittest.skipUnless(TREE_SITTER_AVAILABLE, "Tree-sitter integration dependencies not installed")
class TreeSitterIntegrationTests(unittest.TestCase):
    def setUp(self):
        warnings.filterwarnings(
            "ignore",
            message=r"Language\(path, name\) is deprecated",
            category=FutureWarning,
        )
        self.parser = TreeSitterSyntaxParser()

    def test_real_python_java_and_cpp_files_produce_true_syntax_trees(self):
        samples = (
            (
                "python",
                SOURCE_ROOT
                / "envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba"
                / "ykman/_cli/__init__.py",
                "module",
            ),
            (
                "java",
                SOURCE_ROOT
                / "executionagent-apache-commons-csv-2d44689ec75e"
                / "src/main/java/org/apache/commons/csv/CSVFormat.java",
                "program",
            ),
            (
                "cpp",
                SOURCE_ROOT
                / "executionagent-ccache-ccache-7f3e822efb1b"
                / "src/ccache/ccache.cpp",
                "translation_unit",
            ),
        )
        for language, path, expected_root_type in samples:
            with self.subTest(language=language):
                self.assertTrue(path.is_file(), path)

                root = self.parser.parse_bytes(path, path.read_bytes())

                self.assertEqual(root.type, expected_root_type)
                self.assertGreater(root.named_child_count, 0)

    def test_all_declared_grammars_load_and_parse_minimal_source(self):
        samples = (
            ("x.sh", b"echo ok\n", "bash"),
            ("x.c", b"int main(void){return 0;}\n", "c"),
            ("x.cpp", b"int main(){return 0;}\n", "cpp"),
            ("x.cs", b"class C {}\n", "c_sharp"),
            ("x.go", b"package main\nfunc main() {}\n", "go"),
            ("X.java", b"class X {}\n", "java"),
            ("x.js", b"function x() {}\n", "javascript"),
            ("x.kt", b"fun main() {}\n", "kotlin"),
            ("x.php", b"<?php function x() {} ?>\n", "php"),
            ("x.py", b"def x():\n    pass\n", "python"),
            ("x.sql", b"SELECT 1;\n", "sql"),
            ("x.rs", b"fn main() {}\n", "rust"),
            ("x.rb", b"def x\nend\n", "ruby"),
            ("x.ts", b"function x(): void {}\n", "typescript"),
            ("x.tsx", b"const x = <div />;\n", "tsx"),
            ("x.yml", b"key: value\n", "yaml"),
        )
        for filename, source, language in samples:
            with self.subTest(language=language):
                path = Path(filename)

                root = self.parser.parse_bytes(path, source)

                self.assertEqual(self.parser.language_for(path), language)
                self.assertFalse(root.has_error)

    def test_real_commons_csv_subtree_builds_queryable_declaration_graph(self):
        workspace = (
            SOURCE_ROOT
            / "executionagent-apache-commons-csv-2d44689ec75e"
            / "src/main/java/org/apache/commons/csv"
        )
        self.assertTrue(workspace.is_dir(), workspace)
        graph = RepositoryKnowledgeGraphBuilder(
            self.parser,
            KnowledgeGraphBuildConfig(
                max_files=8,
                max_ast_depth=8,
                max_ast_nodes_per_file=300,
                max_total_nodes=3_000,
            ),
        ).build(SourceReference("dataset://apache/commons-csv", "2d44689ec75e"), workspace)

        declarations = [
            node
            for node in graph.nodes
            if node.kind is KnowledgeNodeKind.DECLARATION
        ]
        self.assertEqual(graph.metadata["language_file_counts"], {"java": 8})
        self.assertTrue(declarations)
        self.assertTrue(
            any(
                node.node_type in {"class_declaration", "method_declaration"}
                for node in declarations
            )
        )
        self.assertLessEqual(len(graph.nodes), 3_000)


if __name__ == "__main__":
    unittest.main()
