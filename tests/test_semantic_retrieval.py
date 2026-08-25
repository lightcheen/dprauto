import tempfile
import unittest
from pathlib import Path

from dprauto.adapters.intelligence import CodeAwareHashingEncoder
from dprauto.agent.context import AgentContextManager
from dprauto.agent.models import ToolContext, ToolResult
from dprauto.agent.tools import QueryRepositoryContextTool, ToolRegistry
from dprauto.application.intelligence import (
    RepositoryContextRetrievalService,
    create_repository_context_retrieval,
)
from dprauto.config import AgentConfig
from dprauto.domain.models import SourceReference
from dprauto.errors import ModelValidationError, ToolExecutionError
from dprauto.intelligence import (
    KnowledgeEdge,
    KnowledgeEdgeKind,
    KnowledgeNode,
    KnowledgeNodeKind,
    RepositoryKnowledgeGraph,
    SemanticSearchQuery,
)
from dprauto.intelligence.builder import (
    KnowledgeGraphBuildConfig,
    RepositoryKnowledgeGraphBuilder,
)
from dprauto.intelligence.retrieval import (
    MultiTurnRepositoryContext,
    RepositoryRetrievalConfig,
    RepositorySemanticRetriever,
)
from dprauto.ports import SemanticEncoder


def _dot(left, right):
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def repository_graph():
    root = KnowledgeNode("root", KnowledgeNodeKind.REPOSITORY, ".")
    test_file = KnowledgeNode(
        "test-file",
        KnowledgeNodeKind.FILE,
        "tests/conftest.py",
        language="python",
    )
    optional_import = KnowledgeNode(
        "optional-import",
        KnowledgeNodeKind.DECLARATION,
        "tests/conftest.py",
        language="python",
        node_type="import_statement",
        symbol="sybil",
        start_line=3,
        end_line=3,
        text="from sybil import Sybil",
        metadata={"role": "import"},
    )
    settings_file = KnowledgeNode(
        "settings-file",
        KnowledgeNodeKind.FILE,
        "tests/settings.py",
        language="python",
    )
    settings = KnowledgeNode(
        "settings",
        KnowledgeNodeKind.AST,
        "tests/settings.py",
        language="python",
        node_type="assignment",
        start_line=8,
        end_line=8,
        text='os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"',
    )
    readme_file = KnowledgeNode(
        "readme-file",
        KnowledgeNodeKind.FILE,
        "README.md",
        language="text",
    )
    service_doc = KnowledgeNode(
        "service-doc",
        KnowledgeNodeKind.TEXT,
        "README.md",
        language="text",
        node_type="text_chunk",
        start_line=20,
        end_line=22,
        text="Start the PostgreSQL service and install the psycopg driver before tests.",
    )
    nodes = (
        root,
        test_file,
        optional_import,
        settings_file,
        settings,
        readme_file,
        service_doc,
    )
    edges = (
        KnowledgeEdge("root", "test-file", KnowledgeEdgeKind.CONTAINS),
        KnowledgeEdge("test-file", "optional-import", KnowledgeEdgeKind.HAS_AST),
        KnowledgeEdge("test-file", "optional-import", KnowledgeEdgeKind.DECLARES),
        KnowledgeEdge("root", "settings-file", KnowledgeEdgeKind.CONTAINS),
        KnowledgeEdge("settings-file", "settings", KnowledgeEdgeKind.HAS_AST),
        KnowledgeEdge("root", "readme-file", KnowledgeEdgeKind.CONTAINS),
        KnowledgeEdge("readme-file", "service-doc", KnowledgeEdgeKind.HAS_TEXT),
    )
    return RepositoryKnowledgeGraph(
        "semantic-graph",
        SourceReference("fixture://semantic"),
        "fingerprint",
        "root",
        nodes,
        edges,
    )


class FakeSyntaxNode:
    def __init__(self, node_type, start, end, children=()):
        self.type = node_type
        self.start_byte = start
        self.end_byte = end
        self.start_point = (0, start)
        self.end_point = (0, end)
        self.named_children = tuple(children)
        self.has_error = False

    def child_by_field_name(self, field_name):
        return None


class FakeSyntaxParser:
    def supports_path(self, path):
        return path.suffix == ".py"

    def language_for(self, path):
        return "python"

    def parse_bytes(self, path, content):
        imported = FakeSyntaxNode("import_statement", 0, len(content))
        return FakeSyntaxNode("module", 0, len(content), (imported,))


class SemanticRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.encoder = CodeAwareHashingEncoder(dimensions=512)
        self.config = RepositoryRetrievalConfig(
            max_index_nodes=100,
            max_results_per_path=2,
            max_session_turns=2,
            max_session_nodes=8,
            max_context_characters=2_000,
            max_context_characters_per_hit=500,
        )
        self.retriever = RepositorySemanticRetriever(self.encoder, self.config)
        self.graph = repository_graph()

    def test_code_aware_encoder_is_deterministic_synonym_aware_and_a_port(self):
        self.assertIsInstance(self.encoder, SemanticEncoder)
        query = self.encoder.encode("optional dependency")

        self.assertEqual(query, self.encoder.encode("optional dependency"))
        self.assertGreater(
            _dot(query, self.encoder.encode("plugin requirement")),
            _dot(query, self.encoder.encode("screen color rendering")),
        )

    def test_hybrid_search_finds_code_configuration_and_documentation(self):
        cases = (
            ("missing optional package import", "tests/conftest.py"),
            ("Django configuration environment", "tests/settings.py"),
            ("database daemon SQL driver", "README.md"),
        )
        for text, expected_path in cases:
            with self.subTest(text=text):
                hits = self.retriever.search(
                    self.graph,
                    SemanticSearchQuery(text, limit=4),
                )

                self.assertTrue(hits)
                self.assertIn(expected_path, {hit.node.path for hit in hits})
                self.assertTrue(all(0.0 <= hit.score <= 1.0 for hit in hits))

    def test_filters_and_graph_relationship_scores_are_applied(self):
        hits = self.retriever.search(
            self.graph,
            SemanticSearchQuery(
                "sybil test dependency",
                kinds=(KnowledgeNodeKind.DECLARATION,),
                path_prefix="tests",
                languages=("Python",),
            ),
        )

        self.assertEqual(hits[0].node.node_id, "optional-import")
        self.assertGreater(hits[0].graph_score, 0.0)
        self.assertTrue(hits[0].related_nodes)

    def test_small_index_budget_preserves_code_config_and_document_kinds(self):
        retriever = RepositorySemanticRetriever(
            self.encoder,
            RepositoryRetrievalConfig(
                max_index_nodes=4,
                max_results_per_path=2,
            ),
        )

        hits = retriever.search(
            self.graph,
            SemanticSearchQuery(
                "PostgreSQL service driver",
                kinds=(KnowledgeNodeKind.TEXT,),
                limit=2,
            ),
        )

        self.assertEqual(hits[0].node.node_id, "service-doc")

    def test_multi_turn_context_accumulates_only_novel_bounded_nodes(self):
        sessions = MultiTurnRepositoryContext(self.retriever, self.config)
        first = sessions.query(
            "session-1",
            self.graph,
            SemanticSearchQuery("test dependency and settings", limit=2),
        )
        second = sessions.query(
            "session-1",
            self.graph,
            SemanticSearchQuery("test dependency and settings", limit=2),
        )

        first_ids = {hit.node.node_id for hit in first.turn.hits}
        second_ids = {hit.node.node_id for hit in second.turn.hits}
        self.assertTrue(first_ids)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertEqual(second.turn.number, 2)
        self.assertLessEqual(len(second.session.context), 2_000)
        self.assertEqual(
            len(second.session.seen_node_ids),
            len(first_ids | second_ids),
        )
        with self.assertRaisesRegex(ModelValidationError, "maximum query turns"):
            sessions.query(
                "session-1",
                self.graph,
                SemanticSearchQuery("another query"),
            )


class RepositoryContextToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "tests").mkdir()
        (self.root / "tests/conftest.py").write_text(
            "from sybil import Sybil\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "Tests require an optional documentation dependency.\n",
            encoding="utf-8",
        )
        (self.root / "credentials.json").write_text(
            '{"token":"do-not-index-this-secret"}\n',
            encoding="utf-8",
        )
        config = RepositoryRetrievalConfig(
            max_index_nodes=100,
            max_session_turns=3,
            max_context_characters=2_000,
            max_context_characters_per_hit=500,
        )
        self.tool = QueryRepositoryContextTool(
            RepositoryKnowledgeGraphBuilder(
                FakeSyntaxParser(),
                KnowledgeGraphBuildConfig(max_files=20, max_total_nodes=100),
            ),
            MultiTurnRepositoryContext(
                RepositorySemanticRetriever(CodeAwareHashingEncoder(), config),
                config,
            ),
        )
        self.context = ToolContext("semantic-run", 0, str(self.root))

    def test_tool_is_read_only_schema_checked_and_retains_multi_turn_session(self):
        registry = ToolRegistry((self.tool,))
        first = registry.invoke(
            "query_repository_context",
            {"query": "missing sybil package", "limit": 2},
            self.context,
        )
        second = registry.invoke(
            "query_repository_context",
            {"query": "optional test dependency", "limit": 2},
            self.context,
        )

        self.assertEqual(registry.specifications[self.tool.name]["effect"], "observe")
        self.assertEqual(first.data["turn"], 1)
        self.assertEqual(second.data["turn"], 2)
        self.assertEqual(first.data["session_id"], second.data["session_id"])
        self.assertTrue(first.data["hits"])
        self.assertIn("tests/conftest.py", first.data["context"])
        self.assertNotIn("do-not-index-this-secret", first.data["context"])
        with self.assertRaises(ToolExecutionError):
            registry.invoke(
                "query_repository_context",
                {"query": "x", "limit": 99},
                self.context,
            )

    def test_sensitive_configuration_is_excluded_from_semantic_graph(self):
        result = self.tool.invoke(
            {"query": "do-not-index-this-secret", "limit": 4},
            ToolContext("sensitive-run", 0, str(self.root)),
        )

        self.assertNotIn("do-not-index-this-secret", result.data["context"])

    def test_semantic_context_survives_agent_evidence_bounding(self):
        result = self.tool.invoke(
            {"query": "missing sybil test dependency", "limit": 2},
            self.context,
        )
        manager = AgentContextManager(
            AgentConfig(max_evidence_characters=2_000)
        )

        evidence = manager.build_evidence_pack(
            (result,),
            rounds=1,
            action_count=1,
            completed=True,
            stop_reason="enough context",
        )

        self.assertEqual(evidence.records[0].tool, "query_repository_context")
        self.assertIn("tests/conftest.py", evidence.records[0].data["context"])

    def test_tool_rejects_unsupported_node_kinds(self):
        with self.assertRaisesRegex(ToolExecutionError, "file, ast"):
            self.tool.invoke(
                {"query": "repository", "kinds": ["repository"]},
                self.context,
            )

    def test_application_service_indexes_then_queries_stored_graph(self):
        service = create_repository_context_retrieval(
            graph_config=KnowledgeGraphBuildConfig(max_files=20, max_total_nodes=100),
            retrieval_config=RepositoryRetrievalConfig(
                max_index_nodes=100,
                max_context_characters=2_000,
                max_context_characters_per_hit=500,
            ),
            syntax_parser=FakeSyntaxParser(),
        )

        self.assertIsInstance(service, RepositoryContextRetrievalService)
        graph = service.index(SourceReference("fixture://service"), self.root)
        result = service.query(
            graph.graph_id,
            "application-session",
            SemanticSearchQuery("missing sybil package", limit=2),
        )

        self.assertTrue(result.turn.hits)
        self.assertIn("tests/conftest.py", result.session.context)


if __name__ == "__main__":
    unittest.main()
