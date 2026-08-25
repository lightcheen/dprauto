import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.intelligence import (
    InMemoryKnowledgeGraphStore,
    Neo4jKnowledgeGraphStore,
    TreeSitterSyntaxParser,
)
from dprauto.application.intelligence import (
    RepositoryIntelligenceService,
    create_repository_intelligence,
)
from dprauto.domain.models import SourceReference
from dprauto.intelligence import (
    KnowledgeEdge,
    KnowledgeEdgeKind,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeQuery,
    RepositoryKnowledgeGraph,
)
from dprauto.intelligence.builder import (
    KnowledgeGraphBuildConfig,
    RepositoryKnowledgeGraphBuilder,
)
from dprauto.ports.intelligence import KnowledgeGraphStore, SyntaxTreeParser


class FakeSyntaxNode:
    def __init__(
        self,
        node_type,
        start_byte,
        end_byte,
        *,
        start_point=(0, 0),
        end_point=(0, 0),
        children=(),
        name_node=None,
        has_error=False,
    ):
        self.type = node_type
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = start_point
        self.end_point = end_point
        self.named_children = tuple(children)
        self._name_node = name_node
        self.has_error = has_error

    def child_by_field_name(self, field_name):
        return self._name_node if field_name == "name" else None


class FakeSyntaxParser:
    def supports_path(self, path):
        return path.suffix == ".py"

    def language_for(self, path):
        return "python"

    def parse_bytes(self, path, content):
        name_start = content.index(b"demo")
        name = FakeSyntaxNode(
            "identifier",
            name_start,
            name_start + 4,
            start_point=(0, 4),
            end_point=(0, 8),
        )
        declaration = FakeSyntaxNode(
            "function_definition",
            0,
            len(content),
            start_point=(0, 0),
            end_point=(1, 0),
            children=(name,),
            name_node=name,
        )
        return FakeSyntaxNode(
            "module",
            0,
            len(content),
            start_point=(0, 0),
            end_point=(1, 0),
            children=(declaration,),
        )


class KnowledgeGraphTests(unittest.TestCase):
    def _build_fixture(self, root):
        (root / "src").mkdir()
        (root / "src/example.py").write_text(
            "def demo():\n    return 1\n", encoding="utf-8"
        )
        (root / "README.md").write_text(
            "# Demo\n\nConfiguration and test documentation.\n", encoding="utf-8"
        )
        builder = RepositoryKnowledgeGraphBuilder(
            FakeSyntaxParser(),
            KnowledgeGraphBuildConfig(
                max_files=20,
                max_ast_nodes_per_file=20,
                text_chunk_characters=24,
                text_chunk_overlap=4,
            ),
        )
        return builder.build(SourceReference("fixture://graph", "abc123"), root)

    def test_builder_creates_deterministic_file_ast_declaration_and_text_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            graph = self._build_fixture(root)
            rebuilt = RepositoryKnowledgeGraphBuilder(
                FakeSyntaxParser(),
                KnowledgeGraphBuildConfig(
                    max_files=20,
                    max_ast_nodes_per_file=20,
                    text_chunk_characters=24,
                    text_chunk_overlap=4,
                ),
            ).build(SourceReference("fixture://graph", "abc123"), root)

            self.assertEqual(graph.graph_id, rebuilt.graph_id)
            self.assertEqual(graph.nodes, rebuilt.nodes)
            kinds = {node.kind for node in graph.nodes}
            self.assertTrue(
                {
                    KnowledgeNodeKind.REPOSITORY,
                    KnowledgeNodeKind.DIRECTORY,
                    KnowledgeNodeKind.FILE,
                    KnowledgeNodeKind.AST,
                    KnowledgeNodeKind.DECLARATION,
                    KnowledgeNodeKind.TEXT,
                }.issubset(kinds)
            )
            declarations = [
                node for node in graph.nodes if node.kind is KnowledgeNodeKind.DECLARATION
            ]
            self.assertEqual(declarations[0].symbol, "demo")
            self.assertEqual(declarations[0].language, "python")
            edge_kinds = {edge.kind for edge in graph.edges}
            self.assertIn(KnowledgeEdgeKind.HAS_AST, edge_kinds)
            self.assertIn(KnowledgeEdgeKind.PARENT_OF, edge_kinds)
            self.assertIn(KnowledgeEdgeKind.DECLARES, edge_kinds)
            self.assertIn(KnowledgeEdgeKind.HAS_TEXT, edge_kinds)
            self.assertIn(KnowledgeEdgeKind.NEXT_CHUNK, edge_kinds)

    def test_source_change_invalidates_graph_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self._build_fixture(root)
            (root / "README.md").write_text("changed\n", encoding="utf-8")

            changed = RepositoryKnowledgeGraphBuilder(
                FakeSyntaxParser()
            ).build(SourceReference("fixture://graph", "abc123"), root)

            self.assertNotEqual(original.source_fingerprint, changed.source_fingerprint)
            self.assertNotEqual(original.graph_id, changed.graph_id)

    def test_in_memory_store_supports_structured_bounded_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = self._build_fixture(Path(directory))
            store = InMemoryKnowledgeGraphStore()

            self.assertIsInstance(store, KnowledgeGraphStore)
            store.replace(graph)
            matches = store.query(
                graph.graph_id,
                KnowledgeQuery(
                    kinds=(KnowledgeNodeKind.DECLARATION,),
                    path_prefix="src",
                    languages=("Python",),
                    text="demo",
                    limit=1,
                ),
            )

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].symbol, "demo")
            self.assertEqual(store.load(graph.graph_id), graph)
            store.delete(graph.graph_id)
            self.assertIsNone(store.load(graph.graph_id))

    def test_ast_node_budget_is_reported_in_graph_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "many.py").write_text("def demo():\n    return 1\n", encoding="utf-8")
            graph = RepositoryKnowledgeGraphBuilder(
                FakeSyntaxParser(),
                KnowledgeGraphBuildConfig(max_ast_nodes_per_file=1),
            ).build(SourceReference("fixture://bounded"), root)

            self.assertEqual(graph.metadata["ast_truncated_files"], ("many.py",))
            ast_nodes = [
                node
                for node in graph.nodes
                if node.kind in {KnowledgeNodeKind.AST, KnowledgeNodeKind.DECLARATION}
            ]
            self.assertEqual(len(ast_nodes), 1)

    def test_global_node_budget_stops_before_unbounded_repository_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("def demo():\n    return 1\n", encoding="utf-8")
            (root / "b.py").write_text("def demo():\n    return 2\n", encoding="utf-8")
            graph = RepositoryKnowledgeGraphBuilder(
                FakeSyntaxParser(),
                KnowledgeGraphBuildConfig(
                    max_ast_nodes_per_file=10,
                    max_total_nodes=4,
                ),
            ).build(SourceReference("fixture://global-budget"), root)

            self.assertLessEqual(len(graph.nodes), 4)
            self.assertTrue(graph.metadata["graph_truncated"])

    def test_tree_sitter_adapter_implements_port_and_maps_language_family(self):
        parser = TreeSitterSyntaxParser()

        self.assertIsInstance(parser, SyntaxTreeParser)
        self.assertEqual(parser.language_for(Path("Example.java")), "java")
        self.assertEqual(parser.language_for(Path("native.cpp")), "cpp")
        self.assertEqual(parser.language_for(Path("workflow.yml")), "yaml")
        self.assertEqual(parser.language_for(Path("component.tsx")), "tsx")
        self.assertFalse(parser.supports_path(Path("pyproject.toml")))

    def test_application_service_indexes_queries_loads_and_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text(
                "def demo():\n    return 1\n",
                encoding="utf-8",
            )
            store = InMemoryKnowledgeGraphStore()
            service = create_repository_intelligence(
                store=store,
                syntax_parser=FakeSyntaxParser(),
                config=KnowledgeGraphBuildConfig(max_files=10),
            )

            self.assertIsInstance(service, RepositoryIntelligenceService)
            graph = service.index(SourceReference("fixture://service"), root)
            matches = service.query(
                graph.graph_id,
                KnowledgeQuery(
                    kinds=(KnowledgeNodeKind.DECLARATION,),
                    text="demo",
                ),
            )

            self.assertEqual(matches[0].symbol, "demo")
            self.assertEqual(service.load(graph.graph_id), graph)
            service.delete(graph.graph_id)
            self.assertIsNone(service.load(graph.graph_id))


class FakeResult(list):
    def single(self):
        return self[0] if self else None

    def consume(self):
        return None


class FakeTransaction:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return FakeResult()


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute_write(self, callback, *args):
        transaction = FakeTransaction()
        callback(transaction, *args)
        self.driver.transactions.append(transaction)

    def run(self, query, **parameters):
        self.driver.calls.append((query, parameters))
        if "RETURN properties(g) AS graph" in query:
            return FakeResult(self.driver.graph_records)
        if "relationship.kind AS kind" in query:
            return FakeResult(self.driver.edge_records)
        if "RETURN properties(n) AS node" in query:
            return FakeResult(self.driver.query_records)
        return FakeResult()


class FakeDriver:
    def __init__(self):
        self.transactions = []
        self.calls = []
        self.query_records = []
        self.graph_records = []
        self.edge_records = []
        self.closed = False

    def session(self):
        return FakeSession(self)

    def close(self):
        self.closed = True


class Neo4jKnowledgeGraphStoreTests(unittest.TestCase):
    @staticmethod
    def _graph():
        root = KnowledgeNode("root", KnowledgeNodeKind.REPOSITORY, ".")
        file_node = KnowledgeNode(
            "file",
            KnowledgeNodeKind.FILE,
            "src/example.py",
            language="python",
        )
        return RepositoryKnowledgeGraph(
            graph_id="graph-1",
            source=SourceReference("fixture://neo4j", "abc"),
            source_fingerprint="fingerprint",
            root_node_id="root",
            nodes=(root, file_node),
            edges=(KnowledgeEdge("root", "file", KnowledgeEdgeKind.CONTAINS),),
            metadata={"case": "fixture", "files": ("src/example.py",)},
        )

    def test_replace_is_scoped_batched_parameterized_and_single_transaction(self):
        driver = FakeDriver()
        store = Neo4jKnowledgeGraphStore(
            driver,
            batch_size=1,
            initialize_schema=False,
        )

        self.assertIsInstance(store, KnowledgeGraphStore)
        store.replace(self._graph())

        self.assertEqual(len(driver.transactions), 1)
        calls = driver.transactions[0].calls
        self.assertIn("DETACH DELETE n", calls[0][0])
        self.assertEqual(calls[0][1], {"graph_id": "graph-1"})
        node_batches = [
            parameters["nodes"]
            for query, parameters in calls
            if "UNWIND $nodes" in query
        ]
        edge_batches = [
            parameters["edges"]
            for query, parameters in calls
            if "UNWIND $edges" in query
        ]
        self.assertEqual([len(batch) for batch in node_batches], [1, 1])
        self.assertEqual([len(batch) for batch in edge_batches], [1])
        self.assertNotIn("fixture://neo4j", "\n".join(query for query, _ in calls))

    def test_query_uses_bounded_parameters_and_reconstructs_nodes(self):
        driver = FakeDriver()
        driver.query_records = [
            {
                "node": {
                    "graph_id": "graph-1",
                    "node_id": "file",
                    "kind": "file",
                    "path": "src/example.py",
                    "language": "python",
                    "node_type": "",
                    "symbol": "example.py",
                    "start_line": None,
                    "end_line": None,
                    "start_column": None,
                    "end_column": None,
                    "text": "",
                    "metadata_json": "{}",
                }
            }
        ]
        store = Neo4jKnowledgeGraphStore(driver, initialize_schema=False)

        matches = store.query(
            "graph-1",
            KnowledgeQuery(
                kinds=(KnowledgeNodeKind.FILE,),
                path_prefix="src",
                languages=("Python",),
                text="example",
                limit=3,
            ),
        )

        self.assertEqual(matches[0].path, "src/example.py")
        _, parameters = driver.calls[0]
        self.assertEqual(parameters["graph_id"], "graph-1")
        self.assertEqual(parameters["kinds"], ["file"])
        self.assertEqual(parameters["languages"], ["python"])
        self.assertEqual(parameters["limit"], 3)

    def test_load_reconstructs_backend_neutral_graph(self):
        driver = FakeDriver()
        driver.graph_records = [
            {
                "graph": {
                    "graph_id": "graph-1",
                    "source_locator": "fixture://neo4j",
                    "source_revision": "abc",
                    "source_subdirectory": "",
                    "source_fingerprint": "fingerprint",
                    "root_node_id": "root",
                    "metadata_json": (
                        '{"case":"fixture","files":["src/example.py"]}'
                    ),
                }
            }
        ]
        driver.query_records = [
            {
                "node": {
                    "graph_id": "graph-1",
                    "node_id": node.node_id,
                    "kind": node.kind.value,
                    "path": node.path,
                    "language": node.language,
                    "node_type": node.node_type,
                    "symbol": node.symbol,
                    "start_line": node.start_line,
                    "end_line": node.end_line,
                    "start_column": node.start_column,
                    "end_column": node.end_column,
                    "text": node.text,
                    "metadata_json": "{}",
                }
            }
            for node in self._graph().nodes
        ]
        driver.edge_records = [
            {
                "source_id": "root",
                "target_id": "file",
                "kind": "contains",
                "metadata_json": "{}",
            }
        ]
        store = Neo4jKnowledgeGraphStore(driver, initialize_schema=False)

        loaded = store.load("graph-1")

        self.assertEqual(loaded, self._graph())


if __name__ == "__main__":
    unittest.main()
